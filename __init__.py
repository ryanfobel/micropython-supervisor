import _thread
import os
import gc

import uasyncio as asyncio
import ujson as json

from .ota_updater import OTAUpdater

try:
    env = json.load(open('env.json', 'r'))
except:
    print('You must create an env.json file.')


class BaseService():
    def __init__(self):
        self.name = self.__class__.__module__.split('.')[-1]
        self._loop = asyncio.get_event_loop()
        self._state = 'stopped'
        self._task = self.main()
        self._loop.create_task(self._task)

    def update(self):
        if 'GITHUB_URL' in self.env.keys():
            print('GITHUB_URL: %s' % self.env['GITHUB_URL'])
            remote_module_path = self.env['PYTHON_MODULE_PATH'] if 'PYTHON_MODULE_PATH' in self.env else ''
            o = OTAUpdater(self.env['GITHUB_URL'], module_path='services/%s' % self.name,
                           remote_module_path=remote_module_path)
            o.using_network(env['WIFI_SSID'], env['WIFI_PASSWORD'])
            try:
                o.check_for_update_to_install_during_next_reboot()
                o.download_and_install_update_if_available(env['WIFI_SSID'], env['WIFI_PASSWORD'])
            except KeyError:
                print("Couldn't get update info.")
        else:
            print('No env defined for %s' % self.name)

    @property
    def state(self):
        lock = _thread.allocate_lock()
        with lock:
            state = self._state
        return state

    @property
    def env(self):
        return self._env()

    @classmethod
    def _env(cls):
        try:
            return json.load(open('envs/%s/env.json' % cls.__module__.split('.')[-1], 'r'))
        except OSError:
            return {}

    @property
    def version(self):
        try:
            return open('services/%s/.version' % self.name, 'r').read()
        except OSError:
            return ''

    def start(self):
        print('Starting %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'running'

    def stop(self):
        print('Stopping %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'stopped'

    async def main(self):
        while True:
            if self._state == 'running':
                await self.loop()
            else:
                await asyncio.sleep(1)

    # This function runs continuously
    async def loop(self):
        print('[%s] state=%s' % (self.__module__, self.state))
        await asyncio.sleep(10)


class Service(BaseService):
    # Setup
    def __init__(self):
        super().__init__()
        self._loop = asyncio.get_event_loop()
        self._services = {}
        self._init_services()
        gc.collect()
        self._get_updates()
        gc.collect()
        self.start_all_services()

    def _init_services(self):
        # Get a list of all services
        for service in os.listdir('services'):
            if service == '__init__.py' or service.startswith('.'):
                continue

            try:
                if service == 'supervisor':
                    self._services[service] = self
                else:
                    # create new service
                    exec('import %s' % service, locals())
                    self._services[service] = locals()[service].Service()
                print('Initialized %s %s' % (self._services[service].name,
                                             self._services[service].version))
            except Exception as e:
                print('Failed to initialize %s: %s' % (service, e))

        # start the asyncio loop in a background thread
        _thread.start_new_thread(self._loop.run_forever, tuple())

    def _get_updates(self):
        # TO DO: update supervisor
        for name, service in self._services.items():
            print('Check for updates to %s' % name)
            service.update()

    @property
    def status(self):
        return {name: (service.state, service.version) for name, service in self._services.items()}

    def stop_all_services(self):
        for service in self._services.values():
            service.stop()

    def start_all_services(self):
        for service in self._services.values():
            service.start()
