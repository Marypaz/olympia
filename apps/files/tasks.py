from datetime import datetime
import hashlib
import urllib
import urllib2
import uuid

from django.conf import settings

from celeryutils import task
import commonware.log
from tower import ugettext as _

from amo.urlresolvers import reverse
from amo.utils import Message
from addons.models import Addon
from versions.compare import version_int
from versions.models import Version
from .models import File
from .utils import JetpackUpgrader

task_log = commonware.log.getLogger('z.task')
jp_log = commonware.log.getLogger('z.jp.repack')


@task
def extract_file(viewer, **kw):
    msg = Message('file-viewer:%s' % viewer)
    msg.delete()
    task_log.info('[1@%s] Unzipping %s for file viewer.' % (
                  extract_file.rate_limit, viewer))

    try:
        viewer.extract()
    except Exception, err:
        if settings.DEBUG:
            msg.save(_('There was an error accessing file %s. %s.') %
                     (viewer, err))
        else:
            msg.save(_('There was an error accessing file %s.') % viewer)
        task_log.error('[1@%s] Error unzipping: %s' %
                       (extract_file.rate_limit, err))


@task
def migrate_jetpack_versions(ids, **kw):
    # TODO(jbalogh): kill in bug 656997
    for file_ in File.objects.filter(id__in=ids):
        file_.jetpack_version = File.get_jetpack_version(file_.file_path)
        task_log.info('Setting jetpack version to %s for File %s.' %
                      (file_.jetpack_version, file_.id))
        file_.save()


# The version/file creation methods expect a files.FileUpload object.
class FakeUpload(object):

    def __init__(self, path, hash, validation):
        self.path = path
        self.hash = hash
        self.validation = validation


@task
def repackage_jetpack(builder_data, **kw):
    jp_log.info('[1@None] Repackaging jetpack for %s.' % builder_data['file_id'])
    jp_log.info('; '.join('%s: "%s"' % i for i in builder_data.items()))
    msg = lambda s: ('[{file_id}]: ' + s).format(**builder_data)
    upgrader = JetpackUpgrader()

    file_data = upgrader.file(builder_data['file_id'])
    if file_data.get('uuid') != builder_data['request']['uuid']:
        return jp_log.warning(msg("Aborting repack. UUID does not match."))

    if builder_data['result'] != 'success':
        return jp_log.warning(msg('Build not successful. {result}: {msg}'))

    try:
        addon = Addon.objects.get(id=builder_data['addon'])
        old_file = File.objects.get(id=builder_data['file_id'])
    except Exception:
        jp_log.error(msg('Could not find addon or file.'), exc_info=True)
        raise

    # Fetch the file from builder.amo.
    try:
        filepath, headers = urllib.urlretrieve(builder_data['location'])
    except Exception:
        jp_log.error(msg('Could not retrieve {location}.'), exc_info=True)
        raise

    # Figure out the SHA256 hash of the file.
    try:
        hash_ = hashlib.sha256()
        with open(filepath, 'rb') as fd:
            while True:
                chunk = fd.read(8192)
                if not chunk:
                    break
                hash_.update(chunk)
    except Exception:
        jp_log.error(msg('Error hashing file.'), exc_info=True)
        raise

    upload = FakeUpload(path=filepath, hash='sha256:%s' % hash_.hexdigest(),
                        validation=None)
    # TODO: multi-file: have we already created the new version for a different
    # file?
    try:
        new_version = Version.from_upload(upload, addon, [old_file.platform])
        new_file = new_version.all_files[0]
        new_file.status = old_file.status
        new_file.save()
    except Exception, e:
        print e
        jp_log.error(msg('Error creating new version/file.'), exc_info=True)
        raise

    # Sync out the new version.
    addon.update_version()

    upgrader.finish(builder_data['file_id'])
    # TODO: Email author.
    # TODO: don't send editor notifications about the new file.
    # Return the new file to make testing easier.
    return new_file


@task
def start_upgrade(version, file_ids, priority='low', **kw):
    upgrader = JetpackUpgrader()
    files = File.objects.filter(id__in=file_ids).select_related('version')
    now = datetime.now()
    for file_ in files:
        if version_int(version) <= version_int(file_.jetpack_version):
            continue

        jp_log.info('Sending %s to builder for jetpack version %s.'
                    % (file_.id, version))
        # Data stored locally so we can figure out job details and if it should
        # be cancelled.
        data = {'file': file_.id,
                'version': version,
                'time': now,
                'uuid': uuid.uuid4().hex,
                'status': 'Sent to builder',
                'owner': 'bulk'}
        upgrader.file(file_.id, data)

        # Data POSTed to the builder.
        post = {'addon': file_.version.addon_id,
                'file_id': file_.id,
                'priority': priority,
                'secret': settings.BUILDER_SECRET_KEY,
                'location': file_.get_url_path(None, 'builder'),
                'uuid': data['uuid'],
                'pingback': reverse('files.builder-pingback')}
        try:
            urllib2.urlopen(settings.BUILDER_UPGRADE_URL, urllib.urlencode(post))
        except Exception:
            jp_log.error('Could not talk to builder for %s.' % file_.id,
                         exc_info=True)
