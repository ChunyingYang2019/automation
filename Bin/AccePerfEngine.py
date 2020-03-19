import cookielib
import copy, pickle
import httplib
import os
import Queue
import random
import re
import socket
import sys
import time
import urllib2, urlparse
from threading import Thread
import AccePerfResult as results
import ssl
import httplib2
from AccePerfSettings import GENERATE_RESULTS, SOCKET_TIMEOUT, WAITFOR_AGENT_FINISH, SHUFFLE_TESTCASES, COOKIES_ENABLED

class LoadManager(Thread):
    def __init__(self, config, runtime_stats, error_queue):
        Thread.__init__(self)

        socket.setdefaulttimeout(SOCKET_TIMEOUT)  # this affects all socket operations (including HTTP)
        self.running = True
        self.user = config.user
        self.password = config.password
        self.num_agents = config.agents
        self.interval = config.interval
        self.rampup = config.rampup
        self.log_msgs = config.logmsg
        self.runtime_stats = runtime_stats
        self.error_queue = error_queue  # list used like a queue
        self.test_name = config.name

        if config.outdir and config.name:
            self.output_dir = time.strftime(config.outdir + '/' + config.name + '_' + 'results_%Y.%m.%d_%H.%M.%S', time.localtime())
        elif config.outdir:
            self.config.outdir = time.strftime(config.outdir + '/' + 'results_%Y.%m.%d_%H.%M.%S', time.localtime())
        elif config.name:
            self.output_dir = time.strftime('results/' + config.name + '_' + 'results_%Y.%m.%d_%H.%M.%S', time.localtime())
        else:
            self.output_dir = time.strftime('results/results_%Y.%m.%d_%H.%M.%S', time.localtime())

        # initialize/reset stats
        for i in range(self.num_agents):
            self.runtime_stats[i] = StatCollection(0, '', 0, 0, 0, 0, 0, 0)

        self.workload = {
            'num_agents': config.agents,
            'interval': int(config.interval) * 1000,  # convert to millisecs
            'rampup': config.rampup,
            'start_epoch': time.mktime(time.localtime())
        }

        self.results_queue = Queue.Queue()  # result stats get queued up by agent threads
        self.agent_refs = []
        self.msg_queue = []  # list of Request objects


    def run(self):
        self.running = True
        self.agents_started = False
        try:
            os.makedirs(self.output_dir, 0755)
        except OSError:
            self.output_dir = self.output_dir + time.strftime('/results_%Y.%m.%d_%H.%M.%S', time.localtime())
            try:
               os.makedirs(self.output_dir, 0755)
            except OSError:
                sys.stderr.write('ERROR: Can not create output directory\n')
                sys.exit(1)

        # start thread for reading and writing queued results
        self.results_writer = ResultWriter(self.results_queue, self.output_dir)
        self.results_writer.setDaemon(True)
        self.results_writer.start()

        for i in range(self.num_agents):
            spacing = float(self.rampup) / float(self.num_agents)
            if i > 0:  # first agent starts right away
                time.sleep(spacing)
            if self.running:  # in case stop() was called before all agents are started
                agent = LoadAgent(i, self.user, self.password, self.interval, self.log_msgs, self.output_dir, self.runtime_stats, self.error_queue, self.msg_queue, self.results_queue)
                agent.start()
                self.agent_refs.append(agent)
                agent_started_line = 'Started agent ' + str(i + 1)
                if sys.platform.startswith('win'):
                    sys.stdout.write(chr(0x08) * len(agent_started_line))  # move cursor back so we update the same line again
                    sys.stdout.write(agent_started_line)
                else:
                    esc = chr(27) # escape key
                    sys.stdout.write(esc + '[G' )
                    sys.stdout.write(esc + '[A' )
                    sys.stdout.write(agent_started_line + '\n')
        if sys.platform.startswith('win'):
            sys.stdout.write('\n')
        print '\nAll agents running...\n\n'
        self.agents_started = True


    def stop(self):
        self.running = False
        for agent in self.agent_refs:
            agent.stop()

        if WAITFOR_AGENT_FINISH:
            keep_running = True
            while keep_running:
                keep_running = False
                for agent in self.agent_refs:
                    if agent.isAlive():
                        keep_running = True
                        time.sleep(0.1)

        self.results_writer.stop()

        if GENERATE_RESULTS:
            # pickle dictionaries to files for results post-processing
            self.store_for_post_processing(self.output_dir, self.runtime_stats, self.workload)

            # auto-generate results from a new thread when the test is stopped
            self.results_gen = results.ResultsGenerator(self.output_dir, self.test_name)
            self.results_gen.setDaemon(True)
            self.results_gen.start()


    def add_cases(self, caselist):
        for case in caselist:
            self.msg_queue.append(case)


    def store_for_post_processing(self, dir, runtime_stats, workload):
        fh = open(dir + '/agent_detail.dat', 'w')
        pickle.dump(runtime_stats, fh)
        fh.close()
        fh = open(dir + '/workload_detail.dat', 'w')
        pickle.dump(workload, fh)
        fh.close()




class LoadAgent(Thread):  # each Agent/VU runs in its own thread
    def __init__(self, id, user, password, interval, log_msgs, output_dir, runtime_stats, error_queue, msg_queue, results_queue):
        Thread.__init__(self)

        self.running = True
        self.id = id
        self.user = user
        self.password = password
        self.interval = interval
        self.log_msgs = log_msgs
        self.output_dir = output_dir

        self.runtime_stats = runtime_stats  # shared stats dictionary
        self.error_queue = error_queue  # shared error list

        # copy message/request queue and all its request objects so we have a unique copy for each agent.
        # this is really slow when there are a lot (1000+) test cases, so we disable it unless we really need to do this.
        # it is necessary for cookies and randomization to work correctly.
        if (SHUFFLE_TESTCASES or COOKIES_ENABLED):
            self.msg_queue = copy.deepcopy(msg_queue)
        else:
            self.msg_queue = msg_queue

        if SHUFFLE_TESTCASES:  # randomize order of testcases per agent
            random.shuffle(self.msg_queue)

        self.results_queue = results_queue  # shared results queue

        self.count = 0
        self.error_count = 0

        # choose timer to use:
        if sys.platform.startswith('win'):
            self.default_timer = time.clock  # time.clock() is more precise on Windows systems
        else:
            self.default_timer = time.time  # time.time() is more precise on Linux/Unix and most other platforms

        self.trace_logging = False
        if self.log_msgs:
            self.enable_trace_logging()


    def run(self):
        agent_start_time = time.strftime('%H:%M:%S', time.localtime())
        total_latency = 0
        total_connect_latency = 0
        total_bytes = 0

        while self.running:
            self.cookie_jar = cookielib.CookieJar()
            for req in self.msg_queue:
                for repeat in range(req.repeat):
                    if self.running:

                        # send the request message
                        resp, content, req_start_time, req_end_time, connect_end_time = self.send(req)

                        # get times for logging and error display
                        tmp_time = time.localtime()
                        cur_date = time.strftime('%d %b %Y', tmp_time)
                        cur_time = time.strftime('%H:%M:%S', tmp_time)

                        # check verifications and status code for errors
                        is_error = False
                        if int(resp.code) >= 400 or int(resp.code) == 0:
                            is_error = True
                        if not req.verify == '':
                            if not re.search(req.verify, content, re.DOTALL):
                                is_error = True
                        if not req.verify_negative == '':
                            if re.search(req.verify_negative, content, re.DOTALL):
                                is_error = True

                        if is_error:
                            self.error_count += 1
                            error_string = 'Agent %s:  %s - %d %s,  url: %s' % (self.id + 1, cur_time, resp.code, resp.msg, req.url)
                            self.error_queue.append(error_string)
                            log_tuple = (self.id + 1, cur_date, cur_time, req_end_time, req.url.replace(',', ''), resp.code, resp.msg.replace(',', ''))
                            self.log_error('%s,%s,%s,%s,%s,%s,%s' % log_tuple)  # write as csv

                        resp_bytes = len(content)
                        latency = (req_end_time - req_start_time)
                        connect_latency = (connect_end_time - req_start_time)

                        self.count += 1
                        total_bytes += resp_bytes
                        total_latency += latency
                        total_connect_latency += connect_latency

                        # update shared stats dictionary
                        self.runtime_stats[self.id] = StatCollection(resp.code, resp.msg, latency, self.count, self.error_count, total_latency, total_connect_latency, total_bytes)
                        self.runtime_stats[self.id].agent_start_time = agent_start_time

                        # put response stats/info on queue for reading by the consumer (ResultWriter) thread
                        q_tuple = (self.id + 1, cur_date, cur_time, req_end_time, req.url.replace(',', ''), resp.code, resp.msg.replace(',', ''), resp_bytes, latency, connect_latency, req.timer_group)
                        self.results_queue.put(q_tuple)

                        expire_time = (self.interval - latency)
                        if expire_time > 0:
                            time.sleep(expire_time)  # sleep remainder of interval so we keep even pacing

                    else:  # don't go through entire range if stop has been called
                        break


    def stop(self):
        self.running = False
        if self.trace_logging:
            self.disable_trace_logging()


    def send(self, req):
        # timed message send+receive (TTLB)
        req_start_time = self.default_timer()
        h=httplib2.Http()
        h.add_credentials(self.user, self.password)
        if req.httpMethod=='GET':
            myResp, content=h.request(req.url, 'GET')
        elif req.httpMethod=='POST':
            myResp, content=h.request(req.url, 'POST', req.xml, req.header)
        elif req.httpMethod=='PUT':
            myResp, content=h.request(req.url, 'PUT', req.xml)
        connect_end_time = self.default_timer()
        req_end_time = self.default_timer()
        resp = ErrorResponse()
        resp.code=myResp.status
        resp.msg=myResp.reason

        req_end_time = self.default_timer()

        if self.trace_logging:
            # log request/response messages
            self.log_http_msgs(req, resp, content)

        return (resp, content, req_start_time, req_end_time, connect_end_time)


    def log_error(self, txt):
        try:
            error_log = open('%s/agent_%d_errors.log' % (self.output_dir, self.id + 1), 'a')
            error_log.write('%s\n' % txt)
            error_log.flush()
            error_log.close()
        except IOError, e:
            sys.stderr.write('ERROR: Can not write to error log file\n')


    def log_http_msgs(self, req, resp, content):
        self.log_trace('\n\n************************* REQUEST *************************\n\n')
        path = urlparse.urlparse(req.url).path
        if path == '':
            path = '/'
        self.log_trace('%s %s' % (req.method.upper(), path))
        #for header_tuple in request.header_items():
            #self.log_trace('%s: %s' % (header_tuple[0], header_tuple[1]))
        self.log_trace('\n\n************************* RESPONSE ************************\n\n')
        self.log_trace('%s %s' % (resp.code, resp.msg))
        for header in resp.headers:
            self.log_trace('%s: %s' % (header, resp.headers[header]))
        self.log_trace('\n\n%s' % content)


    def log_trace(self, txt):
        self.trace_log.write('%s\n' % txt)
        self.trace_log.flush()


    def enable_trace_logging(self):
        self.trace_log = open('%s/agent_%d.log' % (self.output_dir, self.id + 1), 'w')
        self.trace_logging = True


    def disable_trace_logging(self):
        self.trace_log.flush()
        self.trace_log.close()
        self.trace_logging = False


class ErrorResponse():
    # dummy respone that gets used when we encounter socket or http errors
    def __init__(self):
        self.status = 0
        self.msg = 'Connection error'
        self.headers = {}




class StatCollection():
    def __init__(self, status, reason, latency, count, error_count, total_latency, total_connect_latency, total_bytes):
        self.status = status
        self.reason = reason
        self.latency = latency
        self.count = count
        self.error_count = error_count
        self.total_latency = total_latency
        self.total_connect_latency = total_connect_latency
        self.total_bytes = total_bytes

        self.agent_start_time = None

        if count > 0:
            self.avg_latency = (total_latency / count)
            self.avg_connect_latency = (total_connect_latency / count)
        else:
            self.avg_latency = 0
            self.avg_connect_latency = 0




class ResultWriter(Thread):
    # this thread is for reading queued results and writing them to a log file.
    def __init__(self, results_queue, output_dir):
        Thread.__init__(self)
        self.running = True
        self.results_queue = results_queue
        self.output_dir = output_dir

    def run(self):
        # make sure a results file always exists, even if no agents finished
        fh = open('%s/agent_stats.csv' % self.output_dir, 'w')
        fh.close()

        # The file handle should really be opened once, but this is crashing the
        # Python interpreter when you quit the console with ctrl-c.
        # This is a bug in Python: http://bugs.python.org/issue5160
        # The workaround is to open/close the handle for each write operation.
        # File handles are cheap, so don't worry
        while self.running:
            try:
                q_tuple = self.results_queue.get(False)
                f = open('%s/agent_stats.csv' % self.output_dir, 'a')
                f.write('%s,%s,%s,%s,%s,%d,%s,%d,%f,%f,%s\n' % q_tuple)  # log as csv
                f.flush()
                f.close()
            except Queue.Empty:
                # re-check queue for messages every x sec
                time.sleep(.10)

    def stop(self):
        self.running = False
