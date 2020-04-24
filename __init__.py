import _thread
import os
import gc
import machine
import io
import time
import network
import ntptime
import sys

import uasyncio as asyncio
import ujson as json
import ulogging as logging
from umqtt.robust import MQTTClient

from .ota_updater import OTAUpdater

try:
    env = json.load(open('envs/env.json', 'r'))
except OSError:
    print('You must create an env.json file.')


wifi = network.WLAN(network.STA_IF)
_command_queue = []
_startup_time = None

def get_env(module_name=None):
    try:
        if module_name:
            return json.load(open('envs/%s/env.json' % module_name.split('.')[-1], 'r'))
        else:
            return env
    except OSError:
        return {}


def requires_network(func):
    def wrapper(*args, **kwargs):
        if not wifi.isconnected():
            print('Connecting to network...')
            wifi.active(True)
            wifi.connect(env['WIFI_SSID'], env['WIFI_PASSWORD'])

            # Wait for the connection to be established
            start_time = time.time()
            while not wifi.isconnected() and time.time() - start_time < 5:
                pass

            if wifi.isconnected():
                print('Network config:', wifi.ifconfig())

        if wifi.isconnected():
            return func(*args, **kwargs)
        else:
            print('Can\'t connect to network resource.')
            return None
    return wrapper


# https://github.com/micropython/micropython/pull/3836
# https://docs.micropython.org/en/latest/library/uio.html
class MQTTStream(io.IOBase):
    def __init__(self, client, client_id, print_output=True):
        self.client = client
        self.id = client_id
        self.print_output = print_output
        self.buf = ''
        self.service = ''
        super().__init__()

    def write(self, buf):
        lock = _thread.allocate_lock()
        
        with lock:
            try:
                self.buf += buf
            except TypeError:
                self.buf += buf.decode('utf-8')

            return len(buf)

    async def _process_log_queue(self):
        while True:
            gc.collect()
            
            lock = _thread.allocate_lock()
            
            with lock:
                # Send mqtt messages when we recieve a newline
                while self.buf.find('\n') != -1:
                    newline = self.buf.find('\n')
                    line = self.buf[:newline]
                    self.buf = self.buf[newline + 1:]

                    if _startup_time:
                        utc_time = '%d-%02d-%02dT%02d:%02d:%02d' % time.localtime(time.time() + _startup_time)[:6]
                    else:
                        utc_time = ''

                    uptime = time.time()

                    if len(line.split(':')) >= 3:
                        level, self.service = line.split(':')[:2]
                        message = json.dumps({'utc_time': utc_time,
                                            'level': level,
                                            'uptime': uptime,
                                            'message': ''.join(line.split(':')[2:])})
                        print_message = '[%s] %s - %s - %s - %s' % (self.service, utc_time, uptime, level, message)
                        topic = '%s/%s/logging' % (self.id, self.service)
                    else: # Traceback
                        message = json.dumps({'message': line})
                        print_message = line
                        topic = '%s/%s/exceptions' % (self.id, self.service)

                    try:
                        self.client.publish(topic, message)
                    except Exception as e:
                        print(e)
                        sys.print_exception(e, sys.stderr)

                    # Also print to stdout?
                    if self.print_output:
                        print(print_message)

            await asyncio.sleep(0.1)


class BaseService():
    _logger = None
    _methods = None
    _vars = None

    def __init__(self):
        self.name = self.__class__.__module__.split('.')[-1]
        self._asyncio_loop = asyncio.get_event_loop()
        self._state = 'stopped'
        self._task = self.main()
        self._asyncio_loop.create_task(self._task)
        type(self)._logger = logging.getLogger(self.name)
        
        # Inspect class to get methods and members
        type(self)._methods = [x for x in dir(self.__class__)
                               if eval('callable(%s.Service.%s)' % 
                                       (self.name, x))
                                    and not x.startswith('_')]
        type(self)._vars = [x for x in dir(self.__class__)
                            if x not in self._methods
                                and not x.startswith('_')]

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

    @property
    def hardware_id(self):
        return ''.join([hex(b)[-2:] for b in machine.unique_id()])

    def start(self):
        self._logger.info('Starting %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'running'

    def stop(self):
        self._logger.info('Stopping %s.' % self.name)
        lock = _thread.allocate_lock()
        with lock:
            self._state = 'stopped'
    
    def get_env(self, module=None):
        return get_env(module)

    def set_env(self, new_env, module=None):
        with open('envs/%s/env.json' % module, 'w') as f:
            f.write(json.dumps(new_env))
        return self.get_env(module)

    def update_env(self, update_env, module=None):
        env_dict = get_env(module)
        env_dict.update(update_env)
        with open('envs/%s/env.json' % module, 'w') as f:
            f.write(json.dumps(env_dict))
        return self.get_env(module)

    async def main(self):
        while True:
            if self.state == 'running':
                await self.loop()
            else:
                await asyncio.sleep(1)

    # This function runs continuously
    async def loop(self):
        self._logger.debug('state=%s' % self.state)
        await asyncio.sleep(60)


# Supervisor service
class Service(BaseService):
    # Setup
    def __init__(self):
        super().__init__()
        self.mqtt = None
        self._log_stream = None
        self._asyncio_loop = asyncio.get_event_loop()
        self._services = {}
        self._init_mqtt()
        self._mqtt_connect()
        self._init_logging()
        self._get_updates()
        self._init_services()
        self.start_all_services()
        self._asyncio_loop.create_task(self._update_ntp())

    def _init_mqtt(self):
        MQTT_USER = env['MQTT_USER'] if 'MQTT_USER' in env.keys() else None
        MQTT_PASSWORD = env['MQTT_PASSWORD'] if 'MQTT_PASSWORD' in env.keys() else None

        # Create an mqtt client
        self.mqtt = MQTTClient(self.hardware_id,
                               env['MQTT_HOST'],
                               user=MQTT_USER,
                               password=MQTT_PASSWORD)

        self.mqtt.set_callback(self._mqtt_callback)

    def _init_logging(self):
        LOG_LOCALLY = env['LOG_LOCALLY'] if 'LOG_LOCALLY' in env.keys() else True
        self._log_stream = MQTTStream(self.mqtt,
                                      self.hardware_id,
                                      LOG_LOCALLY)

        self._asyncio_loop.create_task(self._log_stream._process_log_queue())

        # Set the log level based on the global environment variable 'LOG_LEVEL'
        log_level_string = env['LOG_LEVEL'] if 'LOG_LEVEL' in env.keys() else 'DEBUG'

        # Convert the log level `string` to the right enum
        LOG_LEVEL = logging.DEBUG
        for x in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
            if x == log_level_string:
                LOG_LEVEL = eval('logging.%s' % x)
                break

        # Make this the default log stream
        logging.basicConfig(level=LOG_LEVEL, stream=self._log_stream)

        self._asyncio_loop.create_task(self._process_mqtt_messages())

    @staticmethod
    def _mqtt_callback(topic, message):
        topic_path = topic.decode('utf-8').split('/')[1:]
        service, channel = topic_path
        
        # Command router
        if channel == 'commands':
            message = json.loads(message)
            _command_queue.append((service, message))

    async def _process_mqtt_messages(self):
        while True:
            try:
                self.mqtt.check_msg()
            except:
                pass
            await asyncio.sleep(0.1)

            while len(_command_queue):
                service, message = _command_queue.pop(0)
                args = message['args'] if 'args' in message.keys() else ''
                command = 'service.%s(%s)' % (message['command'], args)
                
                # Include `command`, `token`, and `args`, in the response
                response = {'command': message['command'],
                            'token': message['token'],
                            'args': args}

                if message['command'] in self._services[service]._methods:
                    try:
                        response['response'] = eval(command, globals(), {'service': self._services[service]})
                    except Exception as e:
                        response['exception'] = repr(e)
                        self._logger.error('Remote command ("%s") caused an exception.' % command)
                        sys.print_exception(e, self._log_stream)
                else:
                    response['exception'] = 'The specified command is not available.'
                    self._logger.error('Remote command ("%s") caused an exception.' % command)

                self.mqtt.publish('%s/%s/responses' % (self.hardware_id, service), json.dumps(response))

            gc.collect()

    async def _update_ntp(self):
        def update():
            global _startup_time
            try:
                self._logger.info('Get NTP time')
                
                lock = _thread.allocate_lock()
                with lock:
                    _startup_time = ntptime.time() - time.time()

                self._logger.info('_startup_time=%s' % _startup_time)
            except Exception as e:
                self._logger.warning(e)
                sys.print_exception(e, self._log_stream)

        # Try every 10 s until we get an update
        while not _startup_time:
            update()
            await asyncio.sleep(10)
            gc.collect()

        # Afterwords, sync once per day
        while True:
            await asyncio.sleep(60*60*24)
            update()
            gc.collect()

    @requires_network
    def _wifi_connect(self):
        pass

    @requires_network
    def _mqtt_connect(self):
        self.mqtt.connect()
        self.mqtt.subscribe('%s/#' % self.hardware_id)

    @requires_network
    def _get_updates(self):
        reboot_flag = False

        # Get a list of all services
        for service in os.listdir('services'):
            if service == '__init__.py' or service.startswith('.'):
                continue

            self._logger.info('Check for updates to %s' % service)
            service_env = get_env(service)

            if 'GITHUB_URL' in service_env.keys():
                self._logger.info('GITHUB_URL=%s' % service_env['GITHUB_URL'])
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
                except Exception as e:
                    self._logger.error("Couldn't get update info. %s" % repr(e))
                    sys.print_exception(e, self._log_stream)
            else:
                self._logger.error('No env defined for %s' % self.name)
                sys.print_exception(e, self._log_stream)

        if reboot_flag:
            self._logger.info('Updates installed. Rebooting...')
            machine.reset()

    def _init_services(self):
        self._logger.info('root environment = %s' % (json.dumps(self.get_env())))
        
        # Get a list of all services
        for service in os.listdir('services'):
            if service == '__init__.py' or service.startswith('.'):
                continue

            try:
                if service == 'supervisor':
                    self._services[service] = self
                else:
                    # Create new service
                    exec('import %s' % service, locals())
                    self._services[service] = locals()[service].Service()
                self._logger.info('Initialized %s %s' % (self._services[service].name,
                                             self._services[service].version))
                service_env = self.get_env(service)
                self._logger.info('%s environment = %s' % (service, json.dumps(service_env)))
            except Exception as e:
                self._logger.error('Failed to initialize %s: %s' % (service, repr(e)))
                sys.print_exception(e, self._log_stream)

        self._logger.info('Start asyncio background thread.')

        # Start the asyncio loop in a background thread
        _thread.start_new_thread(self._asyncio_loop.run_forever, tuple())

    @property
    def status(self):
        return {name: (service.state, service.version) for name, service in self._services.items()}

    def reset(self):
        machine.reset()

    def stop_all_services(self):
        for service in self._services.values():
            service.stop()

    def start_all_services(self):
        for service in self._services.values():
            service.start()

    # This function runs continuously
    async def loop(self):
        self._logger.debug('state=%s' % self.state)

        # Keep wifi and mqtt connections alive
        try:
            self.mqtt.ping()
        except:
            # _mqtt_connect() requires wifi, so this will also reconnect wifi
            # if necessary
            self._mqtt_connect()

        gc.collect()
        self._logger.info('gc.mem_free()=%s' % gc.mem_free())

        await asyncio.sleep(60)
