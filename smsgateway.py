import argparse
import json
import logging
import multiprocessing
import sys
import threading
import time

import flask
import gammu
import yaml

app = flask.Flask(__name__)
sms_queue = multiprocessing.Queue()

config = {
    'server': {
        'host': '127.0.0.1',
        'port': 9876,
    },
    'gammu': {
        'pin': '',
    },
    'receive_whitelist': [],
    'send_blacklist': []
}


@app.route('/sms', methods=['GET', 'POST'])
def receive_sms():
    n = flask.request.args.get('number')
    if n is not None:
        numbers = n.split(',')
    else:
        logging.error('Could not read phone numbers')
        return json.dumps({'status': 'error', 'message': 'could not read phone numbers'}), 500

    messages = []

    if flask.request.method == 'GET':
        message = flask.request.args.get('message')
        if message:
            messages.append(message)
        else:
            logging.error('Could not read message')
            return json.dumps({'status': 'error', 'message': 'could not read message'}), 500

    if flask.request.method == 'POST':
        try:
            data = json.loads(flask.request.data.decode('utf-8'))
            if 'message' in data and isinstance(data['message'], unicode):
                messages.append(data['message'])
            elif 'messages' in data and isinstance(data['messages'], list):
                for message in data['messages']:
                    if isinstance(message, unicode):
                        messages.append(message)
                    else:
                        logging.warn('Ignored non string value in messages')
            elif 'alerts' in data:
                for alert in data['alerts']:
                    if 'annotations' in alert:
                        messages.append(', '.join(
                            ['%s=%s' % (key, alert['annotations'][key]) for key in alert['annotations'].keys()]))
            else:
                logging.error('Could not read message from JSON')
                return json.dumps({'status': 'error', 'message': 'could not read message from JSON'}), 500
        except ValueError as e:
            logging.error('Could not read POST data (%s)' % e)
            return json.dumps({'status': 'error', 'message': 'could not read JSON data'}), 500

    for number in numbers:
        for message in messages:
            send_sms(number, message)

    return json.dumps({'status': 'ok', 'message': 'messages sent'}), 202


def sms_sender(sm):
    while True:
        data = sms_queue.get()
        try:
            sms_message = {
                'Number': data[0],
                'Text': data[1],
                'SMSC': {'Location': 1},
            }
            sm.SendSMS(sms_message)
            logging.info('Successfully sent SMS message to %s' % data[0])
        except Exception as e:
            logging.error('Error while sending SMS message to %s (%s)' % (data[0], e))


def sms_reader(sm):
    while True:
        sms_messages = []

        try:
            start = True
            status = sm.GetSMSStatus()
            remain = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']
            while remain > 0:
                if start:
                    sms = sm.GetNextSMS(Start=True, Folder=0)
                    start = False
                else:
                    sms = sm.GetNextSMS(Location=sms[0]['Location'], Folder=0)
                sm.DeleteSMS(Location=sms[0]['Location'], Folder=0)
                remain -= len(sms)
                sms_messages.append(sms)
        except Exception as e:
            logging.critical('Could not read SMS messages (%s)' % e)

        for sms in sms_messages:
            number = sms[0]['Number']
            text = sms[0]['Text']
            logging.info('Received SMS message from %s' % number)
            read_sms(number, text)

        time.sleep(1)


def send_sms(number, text):
    if number not in config['send_blacklist']:
        sms_queue.put((number, text))
    else:
        logging.error('Could not sent message to %s, number is blacklisted' % number)


def read_sms(number, text):
    if number in config['receive_whitelist']:
        command = text.lower()
        if command == 'ping':
            send_sms(number, 'PONG')
        elif command == 'pause':
            if number in config['send_blacklist']:
                config['send_blacklist'].remove(number)
                send_sms(number, 'OK')
            else:
                send_sms(number, 'OK')
                config['send_blacklist'].append(number)
        else:
            send_sms(number, 'ERROR')
    else:
        logging.error('Denied access from %s, number is not authorized' % number)


def merge_configs(source, destination):
    for key, value in source.items():
        if isinstance(value, dict):
            node = destination.setdefault(key, {})
            merge_configs(value, node)
        else:
            destination[key] = value

    return destination


def read_config_file(filename):
    try:
        with open(filename, 'r') as stream:
            try:
                yaml_config = yaml.safe_load(stream)
            except yaml.YAMLError as e:
                logging.fatal('Configuration file is not a YAML file')
                return False
    except IOError as e:
        logging.fatal('Could not open configuration file (%s)' % e)
        return False

    try:
        merge_configs(yaml_config, config)
    except TypeError as e:
        logging.fatal('Error while reading configuration (%s)' % e)
        return False

    return True


def init_gammu():
    sm = gammu.StateMachine()
    if 'config' in config['gammu']:
        sm.SetConfig(0, config['gammu']['config'])
    else:
        logging.info('Gammu config not defined.')
        sm.ReadConfig()

    logging.info('Initializing Gammu state machine')
    sm.Init()

    status = sm.GetSecurityStatus()
    if status is None:
        logging.info('PIN code is not required or already entered')
    elif status == 'PIN':
        if config['gammu']['pin'] != '':
            logging.info('Unlocking with PIN code')
            try:
                sm.EnterSecurityCode('PIN', config['gammu']['pin'])
                time.sleep(10)
            except gammu.ERR_SECURITYERROR as e:
                logging.fatal('Failed unlocking with PIN')
                return None
        else:
            logging.fatal('PIN code required but not available in configuration')
            return None
    else:
        logging.fatal('Failed unlocking with PIN,%s is first required' % status)
        return None

    return sm


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format='[%(asctime)s][%(levelname)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    parser = argparse.ArgumentParser(description='SMS gateway for Gammu',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-c', '--config', action='store', dest='config', default='/etc/smsgateway.yml',
                        help='Configuration file')
    args = parser.parse_args()

    if not read_config_file(args.config):
        exit(1)

    logging.info('Starting SMS gateway')

    sm = init_gammu()
    if sm is None:
        exit(1)

    thread_sender = threading.Thread(target=sms_sender, args=(sm,))
    thread_sender.setDaemon(True)
    thread_sender.start()

    thread_reader = threading.Thread(target=sms_reader, args=(sm,))
    thread_reader.setDaemon(True)
    thread_reader.start()

    logging.info('Ready !')
    app.run(config['server']['host'], config['server']['port'])


if __name__ == '__main__':
    main()