import _thread
import os

import uasyncio as asyncio
import ujson as json

from .ota_updater import OTAUpdater

try:
    env = json.load(open('env.json', 'r'))
    print(env)
except:
    print('You must create an env.json file.')


class Supervisor:
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self._services = {}
        self._init_services()
        self._get_updates()
        self.start_all_services()

    def _init_services(self):
        # Get a list of all services
        for service in os.listdir('services'):
            import sys
            sys.path.append('services')

            # create new service
            exec('from %s import Service' % service, globals())
            self._services[service] = Service()
            print('Initialized %s %s' % (self._services[service].__module__,
                                         self._services[service].version))

            # start the asyncio loop in a background thread
            _thread.start_new_thread(self._loop.run_forever, tuple())

    def _get_updates(self):
        # TO DO: update supervisor
        for name, service in self._services.items():
            print('Check for updates to %s' % name)
            service.update()

    @property
    def status(self):
        return {name: (service._state, service.version) for name, service in self._services.items()}

    def stop_all_services(self):
        for name, service in self._services.items():
            print('Stopping %s.' % name)
            lock = _thread.allocate_lock()
            with lock:
                service._state = 'stopped'

    def start_all_services(self):
        for name, service in self._services.items():
            print('Starting %s.' % name)
            lock = _thread.allocate_lock()
            with lock:
                service._state = 'running'


class BaseService:
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self._state = 'stopped'
        self._task = self.main()
        self._loop.create_task(self._task)

    def update(self):
        service_env = json.load(open('services/%s/env.json' % self.__module__, 'r'))
        o = OTAUpdater(service_env['GITHUB_URL'], module='services', main_dir=self.__module__)
        o.using_network(env['WIFI_SSID'], env['WIFI_PASSWORD'])
        o.check_for_update_to_install_during_next_reboot()
        o.download_and_install_update_if_available(env['WIFI_SSID'], env['WIFI_PASSWORD'])

    @property
    def version(self):
        try:
            return open('services/%s/.version' % self.__module__, 'r').read()
        except OSError:
            return ''

    async def main(self):
        while True:
            if self._state == 'running':
                await self.loop()
            else:
                await asyncio.sleep(1)
