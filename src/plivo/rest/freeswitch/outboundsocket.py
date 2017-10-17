# -*- coding: utf-8 -*-
# Copyright (c) 2011 Plivo Team. See LICENSE for details.

from gevent import monkey
monkey.patch_all()

import os.path
import traceback
try:
    import xml.etree.cElementTree as etree
except ImportError:
    from xml.etree.elementtree import ElementTree as etree

PLIVO_FLAG_PREANSWER_ALLOWED = 1
PLIVO_FLAG_RELAY_ANONYMOUS_ANI = 2
PLIVO_FLAG_RELAY_CACODE = 4

MAX_TARGET_URLS = 5


import gevent
import gevent.queue
from gevent import spawn_raw
from gevent.event import AsyncResult

from plivo.utils.encode import safe_str
from plivo.core.freeswitch.eventtypes import Event
from plivo.rest.freeswitch.helpers import HTTPRequest, get_substring
from plivo.core.freeswitch.outboundsocket import OutboundEventSocket
from plivo.rest.freeswitch import elements
from plivo.rest.freeswitch.exceptions import RESTFormatException, \
                                    RESTSyntaxException, \
                                    UnrecognizedElementException, \
                                    RESTRedirectException, \
                                    RESTJumpToSectionException, \
                                    RESTGoToLimitReached, \
                                    RESTTransferException, \
                                    RESTHangup

from plivo.core.watchdog import WatchDog

import re

MAX_REDIRECT = 9999


MAX_GOTO = 100

def parse_params(params, params_sep, key_val_sep):
    param_list = params.split(params_sep)
    param_list = map(lambda x: x.split(key_val_sep, 1), param_list)
    return dict(param_list)


def assimilate_plivo_config(Obj, PlivoConfigStr):
    if not PlivoConfigStr:
	return
    if PlivoConfigStr.endswith(';'):
       # hack for old version of PBX
       PlivoConfigStr = PlivoConfigStr[:-1]
    params = parse_params(PlivoConfigStr, ";", "=")
    if params.has_key('answer_url'):
        Obj.target_url = params['answer_url']
        Obj.error_url = params['answer_url']
    else:
        Obj.error_url = None
    if params.has_key('flags'):
        Obj.flags = int(params['flags'])

def assimilate_ruri_params(Obj, SipReqParams):
    if not SipReqParams:
        return
    params = parse_params(SipReqParams, ";", "=")
    for name in ['CACode', 'CarrierID', 'DNIS']:
        lname = name.lower()
        if params.has_key(lname):
            Obj.session_params[name] = params[lname]

def assimilate_ivr_transfer_params(Obj, ivr_transfer_params):
    Obj.log.debug("assimilate_ivr_transfer_params")
    if not ivr_transfer_params:
        Obj.initial_section = 'main'
        return

    params = parse_params(ivr_transfer_params, ";", "=")
    if params.has_key('initial_section'):
        Obj.initial_section = params['initial_section']
    else:
        Obj.initial_section = 'main'

def assimilate_xml_vars(Obj, PlivoVarsStr):
    if not PlivoVarsStr:
	return
    params = parse_params(PlivoVarsStr, ";", "=")
    Obj.xml_vars.update(params)


def check_transfer_failure_action(Obj):
    channel = Obj.get_channel()
    failure_action = channel.get_header('variable_plivo_transfer_failure_action')
    if failure_action:
        Obj.target_url = failure_action
	Obj.session_params['TransferFailureReason'] = channel.get_header('variable_plivo_transfer_failure_reason')
	Obj.xml_vars['TransferFailureReason'] = Obj.session_params['TransferFailureReason']	


def is_anonymous(n):
    if n.find('Anonymous') >= 0 or n.find('anonymous') >= 0 or n.find('Payphone') >= 0:
       return True
    return False

def find_section(doc, name): 
    for section in doc.findall("Section"):
        if section.get('name') == name:
            return section


def process_url_params(url):
    if url.startswith("http://("):
        pos = url.find(")", 8)
        if pos >= 8:
            return (url[0:7] + url [pos+1:], parse_params(url[8:pos], ",", "="))
    elif url.startswith("https://("):
        pos = url.find(")", 9)
        if pos >= 9:
            return (url[0:8] + url [pos+1:], parse_params(url[9:pos], ",", "="))
    else:
        return (url, None)


class RequestLogger(object):
    """
    Class RequestLogger

    This Class allows a quick way to log a message with request ID
    """
    def __init__(self, logger, request_id=0):
        self.logger = logger
        self.request_id = request_id

    def info(self, msg):
        """Log info level"""
        self.logger.info('(%s) %s' % (self.request_id, safe_str(msg)))

    def warn(self, msg):
        """Log warn level"""
        self.logger.warn('(%s) %s' % (self.request_id, safe_str(msg)))

    def error(self, msg):
        """Log error level"""
        self.logger.error('(%s) %s' % (self.request_id, safe_str(msg)))

    def debug(self, msg):
        """Log debug level"""
        self.logger.debug('(%s) %s' % (self.request_id, safe_str(msg)))



class PlivoOutboundEventSocket(OutboundEventSocket):
    """Class PlivoOutboundEventSocket

    An instance of this class is created every time an incoming call is received.
    The instance requests for a XML element set to execute the call and acts as a
    bridge between Event_Socket and the web application
    """
    WAIT_FOR_ACTIONS = ('playback',
                        'record',
                        'play_and_get_digits',
                        'bridge',
                        'say',
                        'sleep',
                        'speak',
                        'conference',
                        'park',
						'txfax',
						'rxfax',
                       )
    NO_ANSWER_ELEMENTS = (
                          #'Wait',
                          'PreAnswer',
                          'Hangup',
                          'Dial',
			  'Transfer',
                          'Notify',
			  'Redirect',
                         )

    def __init__(self, socket, address,
                 log, cache,
                 default_answer_url=None,
                 default_hangup_url=None,
                 default_http_method='POST',
                 extra_fs_vars=None,
                 auth_id='',
                 auth_token='',
                 request_id=0,
                 trace=False,
                 proxy_url=None,
                 tts_shoutcaster=None,
                 available_tts_voices=None,
                 default_tts_voices=None
		):
        # the request id
        self._request_id = request_id
        # set logger
        self._log = log
        self.log = RequestLogger(logger=self._log, request_id=self._request_id)
        # set auth id/token
        self.key = auth_id
        self.secret = auth_token
        # set all settings empty
        self.xml_response = ''
        self.parsed_element = []
        self.lexed_xml_response = []
        self.target_url = ''
        self.flags = 0
        self.session_params = {}
        self._hangup_cause = ''
        # flag to track current element
        self.current_element = None
        # create queue for waiting actions
        self._action_queue = gevent.queue.Queue(10)
        # set default answer url
        self.default_answer_url = default_answer_url
        # set default hangup_url
        self.default_hangup_url = default_hangup_url
        # set proxy url
        self.proxy_url =  proxy_url
        # set default http method POST or GET
        self.default_http_method = default_http_method
        # identify the extra FS variables to be passed along
        self.extra_fs_vars = extra_fs_vars
        # set answered flag
        self.answered = False

        self.xml_vars = {}

        self.xml_doc = None

        self.goto_count = 0

        self.initial_section = None

        self.domain_flags = 0

        self.scanner = re.Scanner([
           ('{{[^{}]+}}', lambda s, token: self.xml_vars[token[2:-2]] if self.xml_vars.has_key(token[2:-2]) else token),
           ('.', lambda s, token: token)
        ])

        self.dtmf_started = False
        self.cache = cache

        self.tts_shoutcaster = tts_shoutcaster
        #self.log.debug("tts_shoutcaster received as %s" % str(tts_shoutcaster))

        self.available_tts_voices = available_tts_voices

        self.default_tts_voices = default_tts_voices

        # inherits from outboundsocket
        OutboundEventSocket.__init__(self, socket, address, filter=None,
                                     eventjson=True, pool_size=200, trace=trace)

    def interpolate_xml_vars(self, s):
        result, rest = self.scanner.scan(s)
        return ''.join(result)

    def _protocol_send(self, command, args=''):
        """Access parent method _protocol_send
        """
        self.log.debug("Execute: %s args='%s'" % (command, safe_str(args)))
        response = super(PlivoOutboundEventSocket, self)._protocol_send(
                                                                command, args)
        self.log.debug("Response: %s" % str(response))
        if self.has_hangup():
            raise RESTHangup()
        return response

    def _protocol_sendmsg(self, name, args=None, uuid='', lock=False, loops=1):
        """Access parent method _protocol_sendmsg
        """
        self.log.debug("Execute: %s args=%s, uuid='%s', lock=%s, loops=%d" \
                      % (name, safe_str(args), uuid, str(lock), loops))
        response = super(PlivoOutboundEventSocket, self)._protocol_sendmsg(
                                                name, args, uuid, lock, loops)
        self.log.debug("Response: %s" % str(response))
        if self.has_hangup():
            raise RESTHangup()
        return response

    def wait_for_action(self, timeout=3600, raise_on_hangup=False):
        """
        Wait until an action is over
        and return action event.
        """
        self.log.debug("wait for action start")
        try:
            event = self._action_queue.get(timeout=timeout)
            self.log.debug("wait for action end %s" % str(event))
            if raise_on_hangup is True and self.has_hangup():
                self.log.warn("wait for action call hung up !")
                raise RESTHangup()
            return event
        except gevent.queue.Empty:
            if raise_on_hangup is True and self.has_hangup():
                self.log.warn("wait for action call hung up !")
                raise RESTHangup()
            self.log.warn("wait for action end timed out!")
            return Event()


    # In order to "block" the execution of our service until the
    # command is finished, we use a synchronized queue from gevent
    # and wait for such event to come. The on_channel_execute_complete
    # method will put that event in the queue, then we may continue working.
    # However, other events will still come, like for instance, DTMF.
    def on_channel_execute_complete(self, event):
        if event['Application'] in self.WAIT_FOR_ACTIONS:
            # If transfer has begun, put empty event to break current action
            if event['variable_plivo_transfer_progress'] == 'true':
                self._action_queue.put(Event())
            else:
                self._action_queue.put(event)

    def on_channel_hangup_complete(self, event):
        """
        Capture Channel Hangup Complete
        """
        self._hangup_cause = event['Hangup-Cause']
        self.log.info('Event: channel %s has hung up (%s)' %
                      (self.get_channel_unique_id(), self._hangup_cause))
        self.session_params['HangupCause'] = self._hangup_cause
        self.session_params['CallStatus'] = 'completed'
        # Prevent command to be stuck while waiting response
        self._action_queue.put_nowait(Event())

    def on_channel_bridge(self, event):
        # send bridge event to Dial
        if self.current_element == 'Dial':
            self._action_queue.put(event)

    def on_channel_unbridge(self, event):
        # special case to get bleg uuid for Dial
        if self.current_element == 'Dial':
            self._action_queue.put(event)

    def on_detected_speech(self, event):
        # detect speech for GetSpeech
        if self.current_element == 'GetSpeech' \
            and event['Speech-Type'] == 'detected-speech':
            self._action_queue.put(event)

    def on_custom(self, event):
        # case conference event
        if self.current_element == 'Conference':
            # special case to get Member-ID for Conference
            if event['Event-Subclass'] == 'conference::maintenance' \
                and event['Action'] == 'add-member' \
                and event['Unique-ID'] == self.get_channel_unique_id():
                self.log.debug("Entered Conference")
                self._action_queue.put(event)
            # special case for hangupOnStar in Conference
            elif event['Event-Subclass'] == 'conference::maintenance' \
                and event['Action'] == 'kick' \
                and event['Unique-ID'] == self.get_channel_unique_id():
                room = event['Conference-Name']
                member_id = event['Member-ID']
                if room and member_id:
                    self.bgapi("conference %s kick %s" % (room, member_id))
                    self.log.warn("Conference Room %s, member %s pressed '*', kicked now !" \
                            % (room, member_id))
            # special case to send callback for Conference
            elif event['Event-Subclass'] == 'conference::maintenance' \
                and event['Action'] == 'digits-match' \
                and event['Unique-ID'] == self.get_channel_unique_id():
                self.log.debug("Digits match on Conference")
                digits_action = event['Callback-Url']
                digits_method = event['Callback-Method']
                if digits_action and digits_method:
                    params = {}
                    params['ConferenceMemberID'] = event['Member-ID'] or ''
                    params['ConferenceUUID'] = event['Conference-Unique-ID'] or ''
                    params['ConferenceName'] = event['Conference-Name'] or ''
                    params['ConferenceDigitsMatch'] = event['Digits-Match'] or ''
                    params['ConferenceAction'] = 'digits'
                    spawn_raw(self.send_to_url, digits_action, params, digits_method)
            # special case to send callback when Member take the floor in Conference
            # but only if member can speak (not muted)
            elif event['Event-Subclass'] == 'conference::maintenance' \
                and event['Action'] == 'floor-change' \
                and event['Unique-ID'] == self.get_channel_unique_id() \
                and event['Speak'] == 'true':
                self._action_queue.put(event)

        # case dial event
        elif self.current_element == 'Dial':
            if event['Event-Subclass'] == 'plivo::dial' \
                and event['Action'] == 'digits-match' \
                and event['Unique-ID'] == self.get_channel_unique_id():
                self.log.debug("Digits match on Dial")
                digits_action = event['Callback-Url']
                digits_method = event['Callback-Method']
                if digits_action and digits_method:
                    params = {}
                    params['DialDigitsMatch'] = event['Digits-Match'] or ''
                    params['DialAction'] = 'digits'
                    params['DialALegUUID'] = event['Unique-ID']
                    params['DialBLegUUID'] = event['variable_bridge_uuid']
                    spawn_raw(self.send_to_url, digits_action, params, digits_method)

    def has_hangup(self):
        if self._hangup_cause:
            return True
        return False

    def ready(self):
        if self.has_hangup():
            return False
        return True

    def has_answered(self):
        return self.answered

    def get_hangup_cause(self):
        return self._hangup_cause

    def get_extra_fs_vars(self, event):
        params = {}
        if not event or not self.extra_fs_vars:
            return params
        for var in self.extra_fs_vars.split(','):
            var = var.strip()
            if var:
                val = event.get_header(var)
                if val is None:
                    val = ''
                params[var] = val
        return params

    def disconnect(self):
        # Prevent command to be stuck while waiting response
        try:
            self._action_queue.put_nowait(Event())
        except gevent.queue.Full:
            pass
        self.log.debug('Releasing Connection ...')
        super(PlivoOutboundEventSocket, self).disconnect()
        self.log.debug('Releasing Connection Done')

    def run(self):
        try:
            self._run()
        except WatchDog:
            self.log.info('Got WatchDog request')
            self.disconnect()
            self.transport.sockfd.close()
        except RESTHangup:
            self.log.warn('Hangup')
        except Exception, e:
            [ self.log.error(line) for line in \
                        traceback.format_exc().splitlines() ]
            raise e


    def _run(self):
        self.connect()
        #self.resume()
        # Linger to get all remaining events before closing
        self.linger()
        self.myevents()
        self.divert_events('on')
        if self._is_eventjson:
            self.eventjson('CUSTOM conference::maintenance plivo::dial')
        else:
            self.eventplain('CUSTOM conference::maintenance plivo::dial')
        # Set plivo app flag
        self.set('plivo_app=true')
        # Don't hangup after bridge
        self.set('hangup_after_bridge=false')
        channel = self.get_channel()
        self.call_uuid = self.get_channel_unique_id()
        # Set CallerName to Session Params
        self.session_params['CallerName'] = channel.get_header('Caller-Caller-ID-Name') or ''
        # Set CallUUID to Session Params
        self.session_params['CallUUID'] = self.call_uuid
        # Set Direction to Session Params
        self.session_params['Direction'] = channel.get_header('Call-Direction')
        aleg_uuid = ''
        aleg_request_uuid = ''
        forwarded_from = get_substring(':', '@',
                            channel.get_header('variable_sip_h_Diversion'))

        start_time = channel.get_header('Channel-Channel-Created-Time')
        if start_time:
            self.session_params['StartTime'] = start_time

        answer_time = channel.get_header('Channel-Channel-Answered-Time')
        if answer_time:
            self.session_params['AnswerTime'] = answer_time

        assimilate_xml_vars(self, channel.get_header('variable_plivo_xml_vars'))

        self.domain_flags = int(channel.get_header('variable_domain_flags'))

        # Case Outbound
        if self.session_params['Direction'] == 'outbound':
            # Set To / From
            called_no = channel.get_header("variable_plivo_to")
            if not called_no or called_no == '_undef_':
                called_no = channel.get_header('Caller-Destination-Number')
            called_no = called_no or ''
            from_no = channel.get_header("variable_plivo_from")
            if not from_no or from_no == '_undef_':
                from_no = channel.get_header('Caller-Caller-ID-Number') or ''
            # Set To to Session Params
            self.session_params['To'] = called_no.lstrip('+')
            # Set From to Session Params
            self.session_params['From'] = from_no.lstrip('+')

            # Look for variables in channel headers
            aleg_uuid = channel.get_header('Caller-Unique-ID')
            aleg_request_uuid = channel.get_header('variable_plivo_request_uuid')
            # Look for target url in order below :
            #  get plivo_transfer_url from channel var
            #  get plivo_answer_url from channel var
            plivo_config = channel.get_header('variable_plivo_config')

            assimilate_plivo_config(self, plivo_config)
            assimilate_ivr_transfer_params(self, channel.get_header('variable_ivr_transfer_params'))

            check_transfer_failure_action(self)


            domain = channel.get_header('variable_basix_domain')
            domain_id, domain_name = domain.split("*")
            self.session_params['DomainName'] = domain_name
            self.log.info("DomainName %s" % self.session_params['DomainName'])

            if self.target_url:
                self.log.info("Using AnswerUrl %s" % self.target_url)
            else:
                self.log.error('Aborting -- No Call Url found !')
                if not self.has_hangup():
                    self.hangup()
                    raise RESTHangup()
                return
            # Look for a sched_hangup_id
            sched_hangup_id = channel.get_header('variable_plivo_sched_hangup_id')
            # Don't post hangup in outbound direction
            # because it is handled by inboundsocket
            self.default_hangup_url = None
            self.hangup_url = None
            # Set CallStatus to Session Params
            self.session_params['CallStatus'] = 'in-progress'
            # Set answered flag to true in case outbound call
            self.answered = True
            accountsid = channel.get_header("variable_plivo_accountsid")
            if accountsid:
                self.session_params['AccountSID'] = accountsid
        # Case Inbound
        else:
            # Set To / From
            #called_no = channel.get_header("variable_plivo_destination_number")
            #if not called_no or called_no == '_undef_':
            #    called_no = channel.get_header('Caller-Destination-Number')
            #called_no = called_no or ''
            called_no = channel.get_header('variable_sip_to_user')

            from_no = channel.get_header('Caller-Caller-ID-Number') or ''
            # Set To to Session Params
            self.session_params['To'] = called_no.lstrip('+')
            # Set From to Session Params
            self.session_params['From'] = from_no.lstrip('+')
            caller_name = self.session_params['CallerName']

            plivo_config = self.get_var('plivo_config')

            assimilate_plivo_config(self, plivo_config)
            assimilate_ruri_params(self, channel.get_header("variable_sip_req_params"))
            assimilate_ivr_transfer_params(self, channel.get_header('variable_ivr_transfer_params'))

            check_transfer_failure_action(self)


            if is_anonymous(caller_name):
                self.session_params['Anonymous'] = 'true'
                if not (self.flags & PLIVO_FLAG_RELAY_ANONYMOUS_ANI): 
                    self.session_params['From'] = 'Anonymous'
                    self.session_params['CallerName'] = 'Anonymous'
            else:
                self.session_params['Anonymous'] = 'false'
            
            # Look for target url in order below :
            #  get plivo_transfer_url from channel var
            #  get plivo_answer_url from channel var
            #  get default answer_url from config

            domain = channel.get_header('variable_basix_domain')
            domain_id, domain_name = domain.split("*")
            self.session_params['DomainName'] = domain_name
            self.log.info("DomainName %s" % self.session_params['DomainName'])

            if self.target_url:
                self.log.info("Using AnswerUrl %s" % self.target_url)
            elif self.default_answer_url:
                self.target_url = self.default_answer_url
                self.log.info("Using DefaultAnswerUrl %s" % self.target_url)
            else:
                self.log.error('Aborting -- No Call Url found !')
                if not self.has_hangup():
                    self.hangup()
                    raise RESTHangup()
                return
            # Look for a sched_hangup_id
            sched_hangup_id = self.get_var('plivo_sched_hangup_id')
            # Set CallStatus to Session Params
            if channel.get_header('Caller-Channel-Answered-Time') == '0':
                self.session_params['CallStatus'] = 'ringing'
            else:
                self.session_params['CallStatus'] = 'in-progress'

        for k,v in self.session_params.iteritems():
			self.xml_vars[k] = v

        if not sched_hangup_id:
            sched_hangup_id = ''

        # Add more Session Params if present
        if aleg_uuid:
            self.session_params['ALegUUID'] = aleg_uuid
        if aleg_request_uuid:
            self.session_params['ALegRequestUUID'] = aleg_request_uuid
        if sched_hangup_id:
            self.session_params['ScheduledHangupId'] = sched_hangup_id
        if forwarded_from:
            self.session_params['ForwardedFrom'] = forwarded_from.lstrip('+')

        # Remove sched_hangup_id from channel vars
        if sched_hangup_id:
            self.unset('plivo_sched_hangup_id')

        # Run application
        self.log.info('Processing Call')
        try:
            self.process_call()
        except RESTHangup:
            self.log.warn('Channel has hung up, breaking Processing Call')
        except Exception, e:
            err = str(e)
            self.log.error('Processing Call Failure !')
            # If error occurs during xml parsing
            # log exception and break
            self.log.error(err)
            [ self.log.error(line) for line in \
                        traceback.format_exc().splitlines() ]


            self.set('plivo_error=' + err)

            params = {}
            params['CallUUID'] = self.session_params['CallUUID']
            params['CallStatus'] = 'error'
            params['Error'] = err
            
            spawn_raw(self.notify_error, self.error_url, params)

        self.log.info('Processing Call Ended')

    def process_call(self):
        """Method to proceed on the call
        This will fetch the XML, validate the response
        Parse the XML and Execute it
        """
        params = {}
        for x in range(MAX_REDIRECT):
            try:
                # update call status if needed
                if self.has_hangup():
                    self.session_params['CallStatus'] = 'completed'
                # case answer url, add extra vars to http request :
                if x == 0:
                    params = self.get_extra_fs_vars(event=self.get_channel())

                if len(self.lexed_xml_response) == 0:
                    # fetch remote restxml
                    self.fetch_xml(params=params)
                    # check hangup
                    if self.has_hangup():
                        raise RESTHangup()
                    if not self.xml_response or len(self.xml_response) == 0:
                        self.log.warn('No XML Response')
                        if not self.has_hangup():
                            self.hangup()
                        raise RESTHangup()
                    # parse and execute restxml
                    self.lex_xml()

                self.parse_xml()
                self.execute_xml()
                self.log.info('End of RESTXML')
                return
            except RESTRedirectException, redirect:
                # double check channel exists/hung up
                if self.has_hangup():
                    raise RESTHangup()
                res = self.api('uuid_exists %s' % self.get_channel_unique_id())
                if res.get_response() != 'true':
                    self.log.warn("Call doesn't exist !")
                    raise RESTHangup()
                # Set target URL to Redirect URL
                # Set method to Redirect method
                # Set additional params to Redirect params
                self.target_url = redirect.get_url()
                fetch_method = redirect.get_method()
                params = redirect.get_params()
                if not fetch_method:
                    fetch_method = 'POST'
                # Reset all the previous response and element
                self.xml_response = ""
                self.parsed_element = []
                self.lexed_xml_response = []
                self.log.info("Redirecting to %s %s to fetch RESTXML" \
                                        % (fetch_method, self.target_url))
                # If transfer is in progress, break redirect
                xfer_progress = self.get_var('plivo_transfer_progress') == 'true'
                if xfer_progress:
                    self.log.warn('Transfer in progress, breaking redirect to %s %s' \
                                  % (fetch_method, self.target_url))
                    return
                gevent.sleep(0.010)
                continue
            except RESTJumpToSectionException, redirect:
                # double check channel exists/hung up
                if self.goto_count > MAX_GOTO:
                    raise RESTGoToLimitReached("Too many GoTo jumps. Aborting to avoid possible infinite loop.")

                self.goto_count = self.goto_count+1

                if self.has_hangup():
                    raise RESTHangup()
                res = self.api('uuid_exists %s' % self.get_channel_unique_id())
                if res.get_response() != 'true':
                    self.log.warn("Call doesn't exist !")
                    raise RESTHangup()

                self.xml_response = ""
                self.parsed_element = []

                self.lexed_xml_response = []

                section_name = redirect.get_section_name()
                section = find_section(self.xml_doc, section_name)
                if not section:
                    raise RESTFormatException("Section with name='" + section_name + "' not found")
 
                self.recognize_all_elements(section)
                   
                gevent.sleep(0.010)
                continue
            except RESTTransferException, destination: 
                self.session_params['Transfer'] = 'true'
                self.session_params['TransferDestination'] = destination
                self.log.info("End of RESTXML -- Transfer done to %s" % destination)
                return
        self.log.warn('Max Redirect Reached !')

    def fetch_xml(self, params={}, method=None):
        """
        This method will retrieve the xml from the answer_url
        The url result expected is an XML content which will be stored in
        xml_response
        """
        urls = self.target_url.split(',')[:MAX_TARGET_URLS]
        for url in urls:
            (self.xml_response, err) = self.send_to_url(url, params, method)
            if self.xml_response:
                return
        raise Exception(err)

    def send_to_url(self, url=None, params={}, method=None):
        """
        This method will do an http POST or GET request to the Url
        """
        if method is None:
            method = self.default_http_method

        if not url:
            err = "Cannot send XML request %s, no url !" % method
            self.log.warn(err)
            return (None, err)

        params.update(self.session_params)
        for k in self.xml_vars.keys():
            params['var_' + k] = self.xml_vars[k]

        (adjusted_url, url_params) = process_url_params(url)
        timeout = None
        if url_params and url_params.has_key('timeout'):
            timeout = int(url_params['timeout'])

        try:
            http_obj = HTTPRequest(self.key, self.secret, proxy_url=self.proxy_url)
            self.log.warn("CallUUID=%s : Fetching XML from %s with params %s" % (params['CallUUID'], url, params))
            data = http_obj.fetch_response(adjusted_url, params, method, log=self.log, timeout=timeout)
            self.log.warn("CallUUID=%s : Fetched XML = %s" % (params['CallUUID'], data.replace("\n", " ").replace("\r", " ")))
            return (data, None)
        except Exception, e:
            err = "Fetching XML from %s with params %s failed with %s" % (url, params, e)
            self.log.error("CallUUID=%s : %s" % (params['CallUUID'], err))
            return (None, err)

    def notify_error(self, error_url=None, params={}, method='POST'):
        if not error_url:
            return None

        urls = error_url.split(',')[:MAX_TARGET_URLS]
        for url in urls:

            (adjusted_url, url_params) = process_url_params(url)
            timeout = None
            if url_params and url_params.has_key('timeout'):
                timeout = int(url_params['timeout'])

            try:
                http_obj = HTTPRequest(self.key, self.secret, self.proxy_url)
                data = http_obj.fetch_response(adjusted_url, params, method, log=self.log, timeout=timeout)
                return data
            except Exception, e:
                self.log.error("Notifying error to %s %s with %s -- Error: %s"
                                        % (method, url, params, e))
        return None


    def lex_xml(self):
        """
        Validate the XML document and make sure we recognize all Element
        """
        # Parse XML into a doctring
        xml_str = ' '.join(self.xml_response.split())
        try:
            #convert the string into an Element instance
            doc = etree.fromstring(xml_str)
        except Exception, e:
            raise RESTSyntaxException("Invalid RESTXML Response Syntax: %s" \
                        % str(e))

        # Make sure the document has a <Response> root
        if doc.tag != 'Response':
            raise RESTFormatException('No Response Tag Present')

        section = find_section(doc, self.initial_section)
        self.xml_doc = doc
        if section:
            doc = section

        self.recognize_all_elements(doc)

    def recognize_all_elements(self, doc):	
        # Make sure we recognize all the Element in the xml
        for element in doc:
            invalid_element = []
            if not hasattr(elements, element.tag):
                invalid_element.append(element.tag)
            else:
                self.lexed_xml_response.append(element)
            if invalid_element:
                raise UnrecognizedElementException("Unrecognized Element: %s"
	                                                       % invalid_element)

    def parse_xml(self):
        """
        This method will parse the XML
        and add the Elements into parsed_element
        """
        # Check all Elements tag name
        for element in self.lexed_xml_response:
            element_class = getattr(elements, str(element.tag), None)
            element_instance = element_class()
            element_instance.parse_element(element, self.target_url)
            self.parsed_element.append(element_instance)
            # Validate, Parse & Store the nested children
            # inside the main element element
            self.validate_element(element, element_instance)

    def validate_element(self, element, element_instance):
        children = element.getchildren()
        if children and not element_instance.nestables:
            raise RESTFormatException("%s cannot have any children!"
                                            % element_instance.name)
        for child in children:
            if child.tag not in element_instance.nestables:
                raise RESTFormatException("%s is not nestable inside %s"
                                            % (child, element_instance.name))
            else:
                self.parse_children(child, element_instance)

    def parse_children(self, child_element, parent_instance):
        child_element_class = getattr(elements, str(child_element.tag), None)
        child_element_instance = child_element_class()
        child_element_instance.parse_element(child_element, None)
        parent_instance.children.append(child_element_instance)

    def execute_xml(self):
        try:
            while True:
                try:
                    element_instance = self.parsed_element.pop(0)
                except IndexError:
                    self.log.info("No more Elements !")
                    break
                if hasattr(element_instance, 'prepare'):
                    # TODO Prepare element concurrently
                    element_instance.prepare(self)
                # Check if it's an inbound call
                if self.session_params['Direction'] == 'inbound':
                    # Answer the call if element need it
                    if not self.answered and \
                        not element_instance.name in self.NO_ANSWER_ELEMENTS:
                        self.log.debug("Answering because Element %s need it" \
                            % element_instance.name)
                        self.answer()
                        self.answered = True
                        self.set('min_dup_digit_spacing_ms=55')
                        self.start_dtmf()
                        self.dtmf_started = True
                        # After answer, update callstatus to 'in-progress'
                        self.session_params['CallStatus'] = 'in-progress'
                else:
                    if not self.dtmf_started:
                        self.set('min_dup_digit_spacing_ms=55')
                        self.start_dtmf()
                        self.dtmf_started = True


                # execute Element
                element_instance.run(self)
                try:
                    del element_instance
                except:
                    pass
        finally:
            # clean parsed elements
            for element in self.parsed_element:
                element = None
            self.parsed_element = []

        # If transfer is in progress, don't hangup call
        if not self.has_hangup():
            xfer_progress = self.get_var('plivo_transfer_progress') == 'true'
            if not xfer_progress:
                self.log.info('No more Elements, Hangup Now !')
                self.session_params['CallStatus'] = 'completed'
                self.session_params['HangupCause'] = 'NORMAL_CLEARING'
                self.hangup()
            else:
                self.log.info('Transfer In Progress !')

