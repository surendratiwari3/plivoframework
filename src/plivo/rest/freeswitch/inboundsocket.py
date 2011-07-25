# -*- coding: utf-8 -*-
# Copyright (c) 2011 Plivo Team. See LICENSE for details.

from gevent import monkey
monkey.patch_all()

import os.path

from gevent import spawn_raw
from gevent import pool
import gevent.event

from plivo.core.freeswitch.inboundsocket import InboundEventSocket
from plivo.rest.freeswitch.helpers import HTTPRequest, get_substring


EVENT_FILTER = "BACKGROUND_JOB CHANNEL_PROGRESS CHANNEL_PROGRESS_MEDIA CHANNEL_HANGUP_COMPLETE CHANNEL_STATE SESSION_HEARTBEAT CHANNEL_ANSWER"


class RESTInboundSocket(InboundEventSocket):
    """
    Interface between REST API and the InboundSocket
    """
    def __init__(self, server):
        self.server = server
        self.log = self.server.log

        InboundEventSocket.__init__(self, self.get_server().fs_host, self.get_server().fs_port, self.get_server().fs_password, 
                                    filter=EVENT_FILTER, trace=self.get_server()._trace)
        # Mapping of Key: job-uuid - Value: request_uuid
        self.bk_jobs = {}
        # Transfer jobs: call_uuid - Value: inline dptools to execute
        self.xfer_jobs = {}
        # Conference sync jobs
        self.conf_sync_jobs = {}
        # Call Requests
        self.call_requests = {}

    def get_server(self):
        return self.server

    def reload_config(self):
        self.get_server().load_config(reload=True)
        self.log = self.server.log

    def get_extra_fs_vars(self, event):
        params = {}
        if not event or not self.get_server().extra_fs_vars:
            return params
        for var in self.get_server().extra_fs_vars.split(','):
            var = var.strip()
            if var:
                val = event.get_header(var)
                if val is None:
                    val = ''
                params[var] = val
        return params

    def on_background_job(self, event):
        """
        Capture Job Event
        Capture background job only for originate and conference,
        and ignore all other jobs
        """
        job_cmd = event['Job-Command']
        job_uuid = event['Job-UUID']
        if job_cmd == 'originate' and job_uuid:
            try:
                status, reason = event.get_body().split(' ', 1)
            except ValueError:
                return
            request_uuid = self.bk_jobs.pop(job_uuid, None)
            if not request_uuid:
                return
            try:
                call_req = self.call_requests[request_uuid]
            except KeyError:
                return
            # Handle failure case of originate
            # This case does not raise a on_channel_hangup event.
            # All other failures will be captured by on_channel_hangup
            status = status.strip()
            reason = reason.strip()
            if status[:3] != '+OK':
                # In case ring/early state done, just warn
                # releasing call request will be done in hangup event
                if call_req.state_flag in ('Ringing', 'EarlyMedia'):
                    self.log.error("Call Attempt Done (%s) for RequestUUID %s but Failed (%s)" \
                                                    % (call_req.state_flag, request_uuid, reason))
                    return
                # If no more gateways, release call request
                elif not call_req.gateways:
                    self.log.error("Call Failed for RequestUUID %s but No More Gateways (%s)" \
                                                    % (request_uuid, reason))
                    # set an empty call_uuid
                    call_uuid = ''
                    hangup_url = call_req.hangup_url
                    self.set_hangup_complete(request_uuid, call_uuid,
                                             reason, event, hangup_url)
                    return
                # If there are gateways and call request state_flag is not set
                # try again a call
                elif call_req.gateways:
                    self.log.warn("Call Failed without Ringing/EarlyMedia for RequestUUID %s - Retrying Now (%s)" \
                                                    % (request_uuid, reason))
                    self.spawn_originate(request_uuid)
        elif job_cmd == 'conference' and job_uuid:
            result = event.get_body().strip() or ''
            async_res = self.conf_sync_jobs.pop(job_uuid, None)
            if async_res is None:
                return
            elif async_res is True:
                self.log.info("Conference Api (async) Response for JobUUID %s -- %s" % (job_uuid, result))
                return
            async_res.set(result)
            self.log.info("Conference Api (sync) Response for JobUUID %s -- %s" % (job_uuid, result))

    def on_channel_progress(self, event):
        request_uuid = event['variable_plivo_request_uuid']
        direction = event['Call-Direction']
        # Detect ringing state
        if request_uuid and direction == 'outbound':
            try:
                call_req = self.call_requests[request_uuid]
            except (KeyError, AttributeError):
                return
            # only send if not already ringing/early state
            if not call_req.state_flag:
                # set state flag to true
                call_req.state_flag = 'Ringing'
                # clear gateways to avoid retry
                call_req.gateways = []
                called_num = event['Caller-Destination-Number']
                caller_num = event['Caller-Caller-ID-Number']
                self.log.info("Call from %s to %s Ringing for RequestUUID %s" \
                                % (caller_num, called_num, request_uuid))
                # send ring if ring_url found
                ring_url = call_req.ring_url
                if ring_url:
                    params = {
                            'To': called_num,
                            'RequestUUID': request_uuid,
                            'Direction': direction,
                            'CallStatus': 'ringing',
                            'From': caller_num
                        }
                    # add extra params
                    extra_params = self.get_extra_fs_vars(event)
                    if extra_params:
                        params.update(extra_params)
                    spawn_raw(self.send_to_url, ring_url, params)

    def on_channel_progress_media(self, event):
        request_uuid = event['variable_plivo_request_uuid']
        direction = event['Call-Direction']
        # Detect early media state
        # See http://wiki.freeswitch.org/wiki/Early_media#Early_Media_And_Dialing_Out
        if request_uuid and direction == 'outbound':
            try:
                call_req = self.call_requests[request_uuid]
            except (KeyError, AttributeError):
                return
            # only send if not already ringing/early state
            if not call_req.state_flag:
                # set state flag to true
                call_req.state_flag = 'EarlyMedia'
                # clear gateways to avoid retry
                call_req.gateways = []
                called_num = event['Caller-Destination-Number']
                caller_num = event['Caller-Caller-ID-Number']
                self.log.info("Call from %s to %s in EarlyMedia for RequestUUID %s" \
                                % (caller_num, called_num, request_uuid))
                # send ring if ring_url found
                ring_url = call_req.ring_url
                if ring_url:
                    params = {
                            'To': called_num,
                            'RequestUUID': request_uuid,
                            'Direction': direction,
                            'CallStatus': 'ringing',
                            'From': caller_num
                        }
                    # add extra params
                    extra_params = self.get_extra_fs_vars(event)
                    if extra_params:
                        params.update(extra_params)
                    spawn_raw(self.send_to_url, ring_url, params)

    def on_channel_answer(self, event):
        # request Dial callbackUrl if needed
        ck_url = event['variable_plivo_dial_callback_url']
        if not ck_url:
            return
        ck_method = event['variable_plivo_dial_callback_method']
        if not ck_method:
            return
        aleg_uuid = event['variable_plivo_dial_callback_aleg']
        if not aleg_uuid:
            return
        bleg_uuid = event['Unique-ID']
        params = {'DialBLegUUID': bleg_uuid,
                  'DialALegUUID': aleg_uuid,
                  'DialBLegStatus': 'answer',
                  'CallUUID': aleg_uuid
                 }
        spawn_raw(self.send_to_url, ck_url, params, ck_method)
        return

    def on_channel_hangup_complete(self, event):
        """Capture Channel Hangup Complete
        """
        # if plivo_app != 'true', skip this hangup
        plivo_app_flag = event['variable_plivo_app'] == 'true'
        if not plivo_app_flag:
            # request Dial callbackUrl if needed
            ck_url = event['variable_plivo_dial_callback_url']
            if not ck_url:
                return
            ck_method = event['variable_plivo_dial_callback_method']
            if not ck_method:
                return
            aleg_uuid = event['variable_plivo_dial_callback_aleg']
            if not aleg_uuid:
                return
            hangup_cause = event['Hangup-Cause'] or ''
            # don't send http request for B legs losing bridge race
            if hangup_cause == 'LOSE_RACE':
                return
            bleg_uuid = event['Unique-ID']
            params = {'DialBLegUUID': bleg_uuid,
                      'DialALegUUID': aleg_uuid,
                      'DialBLegStatus': 'hangup',
                      'DialBLegHangupCause': hangup_cause,
                      'CallUUID': aleg_uuid
                     }
            spawn_raw(self.send_to_url, ck_url, params, ck_method)
            return

        # Get call direction
        direction = event['Call-Direction']

        # Handle incoming call hangup
        if direction == 'inbound':
            call_uuid = event['Unique-ID']
            reason = event['Hangup-Cause']
            # send hangup
            try:
                self.set_hangup_complete(None, call_uuid, reason, event, None)
            except Exception, e:
                self.log.error(str(e))
        # Handle outgoing call hangup
        else:
            # check if found a request uuid
            # if not, ignore hangup event
            request_uuid = event['variable_plivo_request_uuid']
            if not request_uuid and direction != 'outbound':
                return
            call_uuid = event['Unique-ID']
            reason = event['Hangup-Cause']
            try:
                call_req = self.call_requests[request_uuid]
            except KeyError:
                return
            # If there are gateways to try again, spawn originate
            if call_req.gateways:
                self.log.debug("Call Failed for RequestUUID %s - Retrying (%s)" \
                                % (request_uuid, reason))
                self.spawn_originate(request_uuid)
                return
            # else clean call request
            hangup_url = call_req.hangup_url
            # send hangup
            try:
                self.set_hangup_complete(request_uuid, call_uuid, reason, event, hangup_url)
            except Exception, e:
                self.log.error(str(e))

    def on_channel_state(self, event):
        # When transfer is ready to start,
        # channel goes in state CS_RESET
        if event['Channel-State'] == 'CS_RESET':
            call_uuid = event['Unique-ID']
            xfer = self.xfer_jobs.pop(call_uuid, None)
            if not xfer:
                return
            self.log.info("TransferCall In Progress for %s" % call_uuid)
            # unset transfer progress flag
            self.set_var("plivo_transfer_progress", "false", uuid=call_uuid)
            # really transfer now
            res = self.api("uuid_transfer %s '%s' inline" % (call_uuid, xfer))
            if res.is_success():
                self.log.info("TransferCall Done for %s" % call_uuid)
            else:
                self.log.info("TransferCall Failed for %s: %s" \
                               % (call_uuid, res.get_response()))
        # On state CS_HANGUP, remove transfer job linked to call_uuid
        elif event['Channel-State'] == 'CS_HANGUP':
            call_uuid = event['Unique-ID']
            # try to clean transfer call
            xfer = self.xfer_jobs.pop(call_uuid, None)
            if xfer:
                self.log.warn("TransferCall Aborted (hangup) for %s" % call_uuid)

    def on_session_heartbeat(self, event):
        """Capture every heartbeat event in a session and post info
        """
        params = {}
        answer_seconds_since_epoch = float(event['Caller-Channel-Answered-Time'])/1000000
        # using UTC here .. make sure FS is using UTC also
        params['AnsweredTime'] = str(answer_seconds_since_epoch)
        heartbeat_seconds_since_epoch = float(event['Event-Date-Timestamp'])/1000000
        # using UTC here .. make sure FS is using UTC also
        params['HeartbeatTime'] = str(heartbeat_seconds_since_epoch)
        params['ElapsedTime'] = str(heartbeat_seconds_since_epoch - answer_seconds_since_epoch)
        params['To'] = event['Caller-Destination-Number'].lstrip('+')
        params['From'] = event['Caller-Caller-ID-Number'].lstrip('+')
        params['CallUUID'] = event['Unique-ID']
        params['Direction'] = event['Call-Direction']
        forwarded_from = get_substring(':', '@',
                            event['variable_sip_h_Diversion'])
        if forwarded_from:
            params['ForwardedFrom'] = forwarded_from.lstrip('+')
        if event['Channel-State'] == 'CS_EXECUTE':
            params['CallStatus'] = 'in-progress'
        # RequestUUID through which this call was initiated if outbound
        request_uuid = event['variable_plivo_request_uuid']
        if request_uuid:
            params['RequestUUID'] = request_uuid
            
        self.log.debug("Got Session Heartbeat from Freeswitch: %s" % params)
        
        if self.get_server().call_heartbeat_url:
            self.log.debug("Sending heartbeat to callback: %s" % self.get_server().call_heartbeat_url)
            spawn_raw(self.send_to_url, self.get_server().call_heartbeat_url, params)

    def set_hangup_complete(self, request_uuid, call_uuid, reason, event, hangup_url):
        params = {}
        # add extra params
        params = self.get_extra_fs_vars(event)

        # case incoming call
        if not request_uuid:
            self.log.info("Hangup for Incoming CallUUID %s Completed, HangupCause %s" \
                                                        % (call_uuid, reason))
            # get hangup url
            hangup_url = event['variable_plivo_hangup_url']
            if hangup_url:
                self.log.debug("Using HangupUrl for CallUUID %s" \
                                                        % call_uuid)
            else:
                if self.get_server().default_hangup_url:
                    hangup_url = self.get_server().default_hangup_url
                    self.log.debug("Using HangupUrl from DefaultHangupUrl for CallUUID %s" \
                                                        % call_uuid)
                elif event['variable_plivo_answer_url']:
                    hangup_url = event['variable_plivo_answer_url']
                    self.log.debug("Using HangupUrl from AnswerUrl for CallUUID %s" \
                                                        % call_uuid)
                elif self.get_server().default_answer_url:
                    hangup_url = self.get_server().default_answer_url
                    self.log.debug("Using HangupUrl from DefaultAnswerUrl for CallUUID %s" \
                                                        % call_uuid)
            if not hangup_url:
                self.log.debug("No HangupUrl for Incoming CallUUID %s" % call_uuid)
                return
        # case outgoing call, add params
        else:
            self.log.info("Hangup for Outgoing CallUUID %s Completed, HangupCause %s, RequestUUID %s"
                                        % (call_uuid, reason, request_uuid))
            try:
                self.call_requests[request_uuid] = None
                del self.call_requests[request_uuid]
            except KeyError, AttributeError:
                pass
            self.log.debug("Call Cleaned up for RequestUUID %s" % request_uuid)
            if not hangup_url:
                self.log.debug("No HangupUrl for Outgoing Call %s, RequestUUID %s" % (call_uuid, request_uuid))
                return
            forwarded_from = get_substring(':', '@', event['variable_sip_h_Diversion'])
            aleg_uuid = event['Caller-Unique-ID']
            aleg_request_uuid = event['variable_plivo_request_uuid']
            sched_hangup_id = event['variable_plivo_sched_hangup_id']
            params['RequestUUID'] = request_uuid
            if forwarded_from:
                params['ForwardedFrom'] = forwarded_from.lstrip('+')
            if aleg_uuid:
                params['ALegUUID'] = aleg_uuid
            if aleg_request_uuid:
                params['ALegRequestUUID'] = aleg_request_uuid
            if sched_hangup_id:
                params['ScheduledHangupId'] = sched_hangup_id
        # if hangup url, handle http request
        if hangup_url:
            called_num = event['Caller-Destination-Number']
            caller_num = event['Caller-Caller-ID-Number']
            params['CallUUID'] = call_uuid or ''
            params['HangupCause'] = reason
            params['Direction'] = event['Call-Direction']
            params['To'] = called_num or ''
            params['CallStatus'] = 'completed'
            params['From'] = caller_num or ''
            spawn_raw(self.send_to_url, hangup_url, params)

    def send_to_url(self, url=None, params={}, method=None):
        if method is None:
            method = self.get_server().default_http_method

        if not url:
            self.log.warn("Cannot send %s, no url !" % method)
            return None
        http_obj = HTTPRequest(self.get_server().auth_id, self.get_server().auth_token)
        try:
            data = http_obj.fetch_response(url, params, method)
            self.log.info("Sent to %s %s with %s -- Result: %s"
                                            % (method, url, params, data))
            return data
        except Exception, e:
            self.log.error("Sending to %s %s with %s -- Error: %s"
                                            % (method, url, params, e))
        return None

    def spawn_originate(self, request_uuid):
        try:
            call_req = self.call_requests[request_uuid]
        except KeyError:
            self.log.warn("Call Request not found for RequestUUID %s" % request_uuid)
            return
        try:
            gw = call_req.gateways.pop(0)
        except IndexError:
            self.log.warn("No more Gateways to call for RequestUUID %s" % request_uuid)
            try:
                self.call_requests[request_uuid] = None
                del self.call_requests[request_uuid]
            except KeyError:
                pass
            return

        _options = []
        # Set plivo app flag
        _options.append("plivo_app=true")
        if gw.codecs:
            _options.append("absolute_codec_string=%s" % gw.codecs)
        if gw.timeout:
            _options.append("originate_timeout=%s" % gw.timeout)
        _options.append("ignore_early_media=true")
        options = ','.join(_options)
        outbound_str = "'socket:%s async full' inline" \
                        % self.get_server().fs_out_address

        dial_str = "originate {%s,%s}%s/%s %s" \
            % (gw.extra_dial_string, options, gw.gw, gw.to, outbound_str)

        bg_api_response = self.bgapi(dial_str)
        job_uuid = bg_api_response.get_job_uuid()
        self.bk_jobs[job_uuid] = request_uuid
        if not job_uuid:
            self.log.error("Call Failed for RequestUUID %s -- JobUUID not received" \
                                                            % request_uuid)

    def bulk_originate(self, request_uuid_list):
        if request_uuid_list:
            self.log.info("BulkCall for RequestUUIDs %s" % str(request_uuid_list))
            job_pool = pool.Pool(len(request_uuid_list))
            [ job_pool.spawn(self.spawn_originate, request_uuid)
                                        for request_uuid in request_uuid_list ]
            return True
        self.log.error("BulkCall Failed -- No RequestUUID !")
        return False

    def transfer_call(self, new_xml_url, call_uuid):
        # Set transfer progress flag to prevent hangup
        # when the current outbound_socket flow will end
        self.set_var("plivo_transfer_progress", "true", uuid=call_uuid)
        # Set transfer url
        self.set_var("plivo_transfer_url", new_xml_url, uuid=call_uuid)
        # Link inline dptools (will be run when ready to start transfer)
        # to the call_uuid job
        outbound_str = "socket:%s async full" \
                        % (self.get_server().fs_out_address)
        self.xfer_jobs[call_uuid] = outbound_str
        # Transfer into sleep state a little waiting for real transfer
        res = self.api("uuid_transfer %s 'sleep:5000' inline" % call_uuid)
        if res.is_success():
            self.log.info("TransferCall Spawned for %s" % call_uuid)
            return True
        # On failure, remove the job and log error
        try:
            del self.xfer_jobs[call_uuid]
        except KeyError:
            pass
        self.log.error("TransferCall Spawning Failed for %s : %s" \
                        % (call_uuid, str(res.get_response())))
        return False

    def hangup_call(self, call_uuid="", request_uuid=""):
        if not call_uuid and not request_uuid:
            self.log.error("Call Hangup Failed -- Missing CallUUID or RequestUUID")
            return False
        if call_uuid:
            callid = "CallUUID %s" % call_uuid
            cmd = "uuid_kill %s NORMAL_CLEARING" % call_uuid
        else:  # Use request uuid
            callid = "RequestUUID %s" % request_uuid
            try:
                call_req = self.call_requests[request_uuid]
            except (KeyError, AttributeError):
                self.log.error("Call Hangup Failed -- %s not found" \
                            % (callid))
                return False
            callid = "RequestUUID %s" % request_uuid
            cmd = "hupall NORMAL_CLEARING plivo_request_uuid %s" % request_uuid
        res = self.api(cmd)
        if not res.is_success():
            self.log.error("Call Hangup Failed for %s -- %s" \
                % (callid, res.get_response()))
            return False
        self.log.info("Executed Call Hangup for %s" % callid)
        return True

    def hangup_all_calls(self):
        bg_api_response = self.bgapi("hupall NORMAL_CLEARING")
        job_uuid = bg_api_response.get_job_uuid()
        if not job_uuid:
            self.log.error("Hangup All Calls Failed -- JobUUID not received")
            return False
        self.log.info("Executed Hangup for all calls")
        return True

    def conference_api(self, room=None, command=None, async=True):
        if not command:
            self.log.error("Conference Api Failed -- 'command' is empty")
            return False
        if room:
            cmd = "conference %s %s" % (room, command)
        else:
            cmd = "conference %s" % command
        # async mode
        if async:
            bg_api_response = self.bgapi(cmd)
            job_uuid = bg_api_response.get_job_uuid()
            if not job_uuid:
                self.log.error("Conference Api (async) Failed '%s' -- JobUUID not received" \
                                        % (cmd))
                return False
            self.conf_sync_jobs[job_uuid] = True
            self.log.info("Conference Api (async) '%s' with JobUUID %s" \
                                    % (cmd, job_uuid))
            return True
        # sync mode
        else:
            res = gevent.event.AsyncResult()
            bg_api_response = self.bgapi(cmd)
            job_uuid = bg_api_response.get_job_uuid()
            if not job_uuid:
                self.log.error("Conference Api (async) Failed '%s' -- JobUUID not received" \
                                        % (cmd))
                return False
            self.log.info("Conference Api (sync) '%s' with JobUUID %s" \
                                    % (cmd, job_uuid))
            self.conf_sync_jobs[job_uuid] = res
            try:
                result = res.wait(timeout=120)
                return result
            except gevent.timeout.Timeout:
                self.log.error("Conference Api (sync) '%s' with JobUUID %s -- timeout getting response" \
                                    % (cmd, job_uuid))
                return False
        return False
