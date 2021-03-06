# 1000 x dank aan Evelien die mijn in deze tijden gesteund heeft
# ohja, en er is ook nog tante suker (Jana Dej.) die graag kinderen wilt maar het zelf nog niet beseft

import usocket
import os
import gc
import machine

import ulogging as logging

logger = logging.getLogger('supervisor')


class Version:
    def __init__(self, version_string):
        if not version_string[0] == 'v':
            logger.error('Invalid version number')
            raise ValueError

        version_list = version_string[1:].split('.')
        self.major = version_list[0]
        self.minor = 0
        self.micro = 0
        try:
            self.minor = int(version_list[1])
            self.micro = int(version_list[2])
        except:
            pass
    
    def __lt__(self, other):
        if self.major < other.major:
            return True
        elif (self.major == other.major and
              self.minor < other.minor):
            return True
        elif (self.major == other.major and
              self.minor == other.minor and
              self.micro < other.micro):
            return True
        return False

    def __gt__(self, other):
        return other < self

    def __eq__(self, other):
        if (self.major == other.major and
            self.minor == other.minor and
            self.micro == other.micro):
            return True
        else: 
            return False


class OTAUpdater:
    def __init__(self, github_repo, module_path, remote_module_path=''):
        self.http_client = HttpClient()
        self.github_repo = github_repo.rstrip('/').replace('https://github.com', 'https://api.github.com/repos')
        self.remote_module_path = remote_module_path
        self.module_path = module_path
        self.modules_dir = '/'.join(module_path.split('/')[:-1])
        self.module_name = module_path.split('/')[-1]
        self.update_path = self.modules_dir + '/.' + self.module_name + '_update'

    def check_for_update_to_install_during_next_reboot(self):
        current_version = self.get_version(self.module_path)
        latest_version = self.get_latest_version()

        logger.info('Checking version... ')
        logger.info('\tCurrent version: %s' % current_version)
        logger.info('\tLatest version: %s' % latest_version)
        if Version(latest_version) > Version(current_version):
            logger.info('New version available, will download and install on next reboot')
            if self.update_path.split('/')[-1] not in os.listdir(self.modules_dir):
                os.mkdir(self.update_path)
            with open(self.update_path + '/.version_on_reboot', 'w') as versionfile:
                versionfile.write(latest_version)
            return latest_version
        else:
            return None

    def download_and_install_update_if_available(self):
        if (self.update_path.split('/')[-1]) in os.listdir(self.modules_dir):
            if '.version_on_reboot' in os.listdir(self.update_path):
                latest_version = self.get_version(self.update_path, '.version_on_reboot')
                logger.info('New update found: %s' % latest_version)
                self._download_and_install_update(latest_version)
        else:
            logger.info('No new updates found...')

    def _download_and_install_update(self, latest_version):
        self.download_all_files(self.github_repo + '/contents/', latest_version)
        self.rmtree(self.module_path)
        os.rename(self.update_path + '/.version_on_reboot', self.update_path + '/.version')
        os.rename(self.update_path, self.module_path)
        logger.info('Update installed (%s)' % latest_version)

    def apply_pending_updates_if_available(self):
        if (self.module_name + '_update') in os.listdir(self.modules_path):
            if '.version' in os.listdir(self.update_path):
                pending_update_version = self.get_version(self.update_path)
                logger.info('Pending update found: %s' % pending_update_version)
                self.rmtree(self.module_path)
                os.rename(self.update_path, self.module_path)
                logger.info('Update applied (%s), ready to rock and roll' % pending_update_version)
            else:
                logger.error('Corrupt pending update found, discarding...')
                self.rmtree(self.update_path)
        else:
            logger.info('No pending update found')

    def download_updates_if_available(self):
        current_version = self.get_version(self.module_path)
        latest_version = self.get_latest_version()

        logger.info('Checking version... ')
        logger.info('\tCurrent version: %s' % current_version)
        logger.info('\tLatest version: %s' % latest_version)
        if latest_version > current_version:
            logger.info('Updating...')
            os.mkdir(self.update_path)
            self.download_all_files(self.github_repo + '/contents/' + self.remote_module_path, latest_version)
            with open(self.update_path + '/.version', 'w') as versionfile:
                versionfile.write(latest_version)

            return True
        return False

    def rmtree(self, directory):
        for entry in os.ilistdir(directory):
            is_dir = entry[1] == 0x4000
            if is_dir:
                self.rmtree(directory + '/' + entry[0])

            else:
                os.remove(directory + '/' + entry[0])
        os.rmdir(directory)

    def get_version(self, directory, version_file_name='.version'):
        if version_file_name in os.listdir(directory):
            f = open(directory + '/' + version_file_name)
            version = f.read()
            f.close()
            return version
        return 'v0.0'

    def get_latest_version(self):
        latest_release = self.http_client.get(self.github_repo + '/releases/latest')
        version = latest_release.json()['tag_name']
        latest_release.close()
        return version

    def download_all_files(self, root_url, version):
        file_list = self.http_client.get(root_url + '?ref=refs/tags/' + version)
        for file in file_list.json():
            if self.remote_module_path in file['path']:
                if file['type'] == 'file':
                    download_url = file['download_url']
                    download_path = self.update_path + '/' + file['path'].replace(self.remote_module_path + '/', '')
                    self.download_file(download_url.replace('refs/tags/', ''), download_path)
                elif file['type'] == 'dir':
                    if file['path'] != self.remote_module_path:
                        path = self.update_path + '/' + file['path'].replace(self.remote_module_path + '/', '')
                        os.mkdir(path)
                    self.download_all_files(root_url + '/' + file['name'], version)
        file_list.close()

    def download_file(self, url, path):
        logger.info('\tDownloading: %s' % path)
        with open(path, 'w') as outfile:
            try:
                response = self.http_client.get(url)
                outfile.write(response.text)
            finally:
                response.close()
                outfile.close()
                gc.collect()


class Response:

    def __init__(self, f):
        self.raw = f
        self.encoding = 'utf-8'
        self._cached = None

    def close(self):
        if self.raw:
            self.raw.close()
            self.raw = None
        self._cached = None

    @property
    def content(self):
        if self._cached is None:
            try:
                self._cached = self.raw.read()
            finally:
                self.raw.close()
                self.raw = None
        return self._cached

    @property
    def text(self):
        return str(self.content, self.encoding)

    def json(self):
        import ujson
        return ujson.loads(self.content)


class HttpClient:

    def request(self, method, url, data=None, json=None, headers={}, stream=None):
        try:
            proto, dummy, host, path = url.split('/', 3)
        except ValueError:
            proto, dummy, host = url.split('/', 2)
            path = ''
        if proto == 'http:':
            port = 80
        elif proto == 'https:':
            import ussl
            port = 443
        else:
            raise ValueError('Unsupported protocol: ' + proto)

        if ':' in host:
            host, port = host.split(':', 1)
            port = int(port)

        ai = usocket.getaddrinfo(host, port, 0, usocket.SOCK_STREAM)
        ai = ai[0]

        s = usocket.socket(ai[0], ai[1], ai[2])
        try:
            s.connect(ai[-1])
            if proto == 'https:':
                s = ussl.wrap_socket(s, server_hostname=host)
            s.write(b'%s /%s HTTP/1.0\r\n' % (method, path))
            if not 'Host' in headers:
                s.write(b'Host: %s\r\n' % host)
            # Iterate over keys to avoid tuple alloc
            for k in headers:
                s.write(k)
                s.write(b': ')
                s.write(headers[k])
                s.write(b'\r\n')
            # add user agent
            s.write('User-Agent')
            s.write(b': ')
            s.write('MicroPython OTAUpdater')
            s.write(b'\r\n')
            if json is not None:
                assert data is None
                import ujson
                data = ujson.dumps(json)
                s.write(b'Content-Type: application/json\r\n')
            if data:
                s.write(b'Content-Length: %d\r\n' % len(data))
            s.write(b'\r\n')
            if data:
                s.write(data)

            l = s.readline()
            l = l.split(None, 2)
            status = int(l[1])
            reason = ''
            if len(l) > 2:
                reason = l[2].rstrip()
            while True:
                l = s.readline()
                if not l or l == b'\r\n':
                    break
                if l.startswith(b'Transfer-Encoding:'):
                    if b'chunked' in l:
                        raise ValueError('Unsupported ' + l)
                elif l.startswith(b'Location:') and not 200 <= status <= 299:
                    raise NotImplementedError('Redirects not yet supported')
        except OSError:
            s.close()
            raise

        resp = Response(s)
        resp.status_code = status
        resp.reason = reason
        return resp

    def head(self, url, **kw):
        return self.request('HEAD', url, **kw)

    def get(self, url, **kw):
        return self.request('GET', url, **kw)

    def post(self, url, **kw):
        return self.request('POST', url, **kw)

    def put(self, url, **kw):
        return self.request('PUT', url, **kw)

    def patch(self, url, **kw):
        return self.request('PATCH', url, **kw)

    def delete(self, url, **kw):
        return self.request('DELETE', url, **kw)
