import _thread
import os
import gc
import machine
import io

import uasyncio as asyncio
import ujson as json
import ulogging as logging
from umqtt.robust import MQTTClient

from .ota_updater import OTAUpdater

try:
    env = json.load(open('env.json', 'r'))
except OSError:
    print('You must create an env.json file.')


def get_env(module_name):
    return json.load(open('envs/%s/env.json' % module_name.split('.')[-1], 'r'))


def using_network(func):
    def wrapper(*args, **kwargs):
        import network
        wifi = network.WLAN(network.STA_IF)
        if not wifi.isconnected():
            print('connecting to network...')
            wifi.active(True)
            wifi.connect(env['WIFI_SSID'], env['WIFI_PASSWORD'])
            while not wifi.isconnected():
                pass
            print('network config:', wifi.ifconfig())
        return func(*args, **kwargs)
    return wrapper


# https://github.com/micropython/micropython/pull/3836
# https://docs.micropython.org/en/latest/library/uio.html
class MQTTStream(io.IOBase):
    def __init__(self, client, topic):
        self.client = client
        self.topic = topic
        super().__init__()

    def write(self, buf):
        try:
            self.client.ping()
            self.client.publish(self.topic, buf)
        except:
            pass

        # Also print to stdout
        print(buf)
        return len(buf)


class BaseService():
    logger = None

    def __init__(self):
        self.name = self.__class__.__module__.split('.')[-1]
        self._loop = asyncio.get_event_loop()
        self._state = 'stopped'
        self._task = self.main()
        self._loop.create_task(self._task)
        type(self).logger = logging.getLogger(self.name)

    @property
    def state(self):
        lock = _thread.allocate_lock()
        with lock:
            state = self._state
        return state

    @property
    def env(self):
        return get_env(self.__module__)

    @property
    def version(self):
        try:
            return open('services/%s/.version' % self.name, 'r').read()
        except OSError:
            return ''

    def start(self):
        self.logger.info('Starting %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'running'

    def stop(self):
        self.logger.info('Stopping %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'stopped'

    async def main(self):
        while True:
            if self.state == 'running':
                await self.loop()
            else:
                await asyncio.sleep(1)

    # This function runs continuously
    async def loop(self):
        self.logger.debug('state=%s' % self.state)
        await asyncio.sleep(60)


# Supervisor service
class Service(BaseService):
    # Setup
    def __init__(self):
        super().__init__()
        self.mqtt = None
        self._log_stream = None
        self._loop = asyncio.get_event_loop()
        self._services = {}
        self._init_mqtt()
        self._init_logging()
        self._get_updates()
        self._init_services()
        self.start_all_services()

    @using_network
    def _init_mqtt(self):
        # Create an mqtt client
        self.mqtt = MQTTClient(self.hardware_id,
                               env['MQTT_HOST'])
        self.mqtt.connect()

    def _init_logging(self):
        self._log_stream = MQTTStream(self.mqtt, '%s/logging' % self.hardware_id)

        # make this the default log stream
        logging.basicConfig(level=logging.DEBUG, stream=self._log_stream)

        self._loop.create_task(self._process_mqtt_messages())

    async def _process_mqtt_messages(self):
        while True:
            if self.state == 'running':
                self.mqtt.check_msg()
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(1)

    @using_network
    def _get_updates(self):
        reboot_flag = False

        # Get a list of all services
        for service in os.listdir('services'):
            if service == '__init__.py' or service.startswith('.'):
                continue

            self.logger.info('Check for updates to %s' % service)
            service_env = get_env(service)

            if 'GITHUB_URL' in service_env.keys():
                self.logger.info('GITHUB_URL=%s' % service_env['GITHUB_URL'])
                remote_module_path = service_env['PYTHON_MODULE_PATH'] if 'PYTHON_MODULE_PATH' in service_env else ''
                o = OTAUpdater(service_env['GITHUB_URL'], module_path='services/%s' % service,
                               remote_module_path=remote_module_path)
                try:
                    gc.collect()
                    if o.check_for_update_to_install_during_next_reboot():
                        gc.collect()
                        o.download_and_install_update_if_available()
                        reboot_flag = True
                    gc.collect()                    
                except KeyError:
                    self.logger.error("Couldn't get update info.")
            else:
                self.logger.error('No env defined for %s' % self.name)

        if reboot_flag:
            self.logger.info('Updates installed. Rebooting...')
            machine.reset()

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
                self.logger.info('Initialized %s %s' % (self._services[service].name,
                                             self._services[service].version))
            except Exception as e:
                self.logger.error('Failed to initialize %s: %s' % (service, e))

        self.logger.info('Start asyncio background thread.')

        # start the asyncio loop in a background thread
        _thread.start_new_thread(self._loop.run_forever, tuple())

    @property
    def hardware_id(self):
        return ''.join([hex(b)[-2:] for b in machine.unique_id()])

    @property
    def status(self):
        return {name: (service.state, service.version) for name, service in self._services.items()}

    def stop_all_services(self):
        for service in self._services.values():
            service.stop()

    def start_all_services(self):
        for service in self._services.values():
            service.start()
