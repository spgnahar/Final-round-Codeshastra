#
# Copyright IBM Corp. 2014
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Author: Daniel Bolanos
# Date:   2015

# coding=utf-8
import json                        # json
import threading                   # multi threading
import os                          # for listing directories
import queue as Queue                      # queue used for thread syncronization
import sys                         # system calls
import argparse                    # for parsing arguments
import base64                      # necessary to encode in base64
#                                  # according to the RFC2045 standard
import requests                    # python HTTP requests library

# WebSockets
from autobahn.twisted.websocket import WebSocketClientProtocol, \
    WebSocketClientFactory, connectWS
from twisted.python import log
from twisted.internet import ssl, reactor

try:
    raw_input          # Python 2
except NameError:
    raw_input = input  # Python 3


class Utils:

    @staticmethod
    def getAuthenticationToken(hostname, serviceName, username, password):

        fmt = hostname + "{0}/authorization/api/v1/token?url={0}/{1}/api"
        uri = fmt.format(hostname, serviceName)
        uri = uri.replace("wss://", "https://").replace("ws://", "https://")
        # print(uri)
        auth = (username, password)
        headers = {'Accept': 'application/json'}
        resp = requests.get(uri, auth=auth, verify=False, headers=headers,
                            timeout=(30, 30))
        # print(resp.text)
        jsonObject = resp.json()
        return jsonObject['token']


class WSInterfaceFactory(WebSocketClientFactory):

    def __init__(self, queue, summary, dirOutput, contentType, model,
                 url=None, headers=None, debug=None):

        WebSocketClientFactory.__init__(self, url=url, headers=headers)
        self.queue = queue
        self.summary = summary
        self.dirOutput = dirOutput
        self.contentType = contentType
        self.model = model
        self.queueProto = Queue.Queue()

        self.openHandshakeTimeout = 10
        self.closeHandshakeTimeout = 10

        # start the thread that takes care of ending the reactor so
        # the script can finish automatically (without ctrl+c)
        endingThread = threading.Thread(target=self.endReactor, args=())
        endingThread.daemon = True
        endingThread.start()

    def prepareUtterance(self):

        try:
            utt = self.queue.get_nowait()
            self.queueProto.put(utt)
            return True
        except Queue.Empty:
            # print("getUtterance: no more utterances to process, queue is "
                  # "empty!")
            return False

    def endReactor(self):

        self.queue.join()
        # print("about to stop the reactor!")
        reactor.stop()

    # this function gets called every time connectWS is called (once
    # per WebSocket connection/session)
    def buildProtocol(self, addr):

        try:
            utt = self.queueProto.get_nowait()
            proto = WSInterfaceProtocol(self, self.queue, self.summary,
                                        self.dirOutput, self.contentType)
            proto.setUtterance(utt)
            return proto
        except Queue.Empty:
            # print("queue should not be empty, otherwise this function should "
            #       "not have been called")
            return None


# WebSockets interface to the STT service
#
# note: an object of this class is created for each WebSocket
# connection, every time we call connectWS
class WSInterfaceProtocol(WebSocketClientProtocol):

    def __init__(self, factory, queue, summary, dirOutput, contentType):
        self.factory = factory
        self.queue = queue
        self.summary = summary
        self.dirOutput = dirOutput
        self.contentType = contentType
        self.packetRate = 20
        self.listeningMessages = 0
        self.timeFirstInterim = -1
        self.bytesSent = 0
        self.chunkSize = 2000     # in bytes
        super(self.__class__, self).__init__()
        # print(dirOutput)
        # print("contentType: {} queueSize: {}".format(self.contentType,
                                                     # self.queue.qsize()))

    def setUtterance(self, utt):

        self.uttNumber = utt[0]
        self.uttFilename = utt[1]
        self.summary[self.uttNumber] = {"hypothesis": "",
                                        "status": {"code": "", "reason": ""}}
        self.fileJson = "{}/{}.json.txt".format(self.dirOutput, self.uttNumber)
        try:
            os.remove(self.fileJson)
        except OSError:
            pass

    # helper method that sends a chunk of audio if needed (as required
    # what the specified pacing is)
    def maybeSendChunk(self, data):

        def sendChunk(chunk, final=False):
            self.bytesSent += len(chunk)
            self.sendMessage(chunk, isBinary=True)
            if final:
                self.sendMessage(b'', isBinary=True)

        if (self.bytesSent + self.chunkSize >= len(data)):
            if (len(data) > self.bytesSent):
                sendChunk(data[self.bytesSent:len(data)], True)
                return
        sendChunk(data[self.bytesSent:self.bytesSent + self.chunkSize])
        self.factory.reactor.callLater(0.01, self.maybeSendChunk, data=data)
        return

    # def onConnect(self, response):

    #     # print("onConnect, server connected: {}".format(response.peer))

    def onOpen(self):
        # print("onOpen")
        data = {"action": "start",
                "content-type": str(self.contentType),
                "continuous": True,
                "interim_results": True,
                "inactivity_timeout": 600,
                'max_alternatives': 3,
                'timestamps': True,
                'word_confidence': True}
        # print("sendMessage(init)")
        # send the initialization parameters
        self.sendMessage(json.dumps(data).encode('utf8'))

        # start sending audio right away (it will get buffered in the
        # STT service)
        # print(self.uttFilename)
        with open(str(self.uttFilename), 'rb') as f:
            self.bytesSent = 0
            dataFile = f.read()
        self.maybeSendChunk(dataFile)
        # print("onOpen ends")

    def onMessage(self, payload, isBinary):

        # if isBinary:
        #     print("Binary message received: {0} bytes".format(len(payload)))
        # else:
        #     print(u"Text message received: {0}".format(payload.decode('utf8')))

            # if uninitialized, receive the initialization response
            # from the server
            jsonObject = json.loads(payload.decode('utf8'))
            if 'state' in jsonObject:
                self.listeningMessages += 1
                if self.listeningMessages == 2:
                    # print("sending close 1000")
                    # close the connection
                    self.sendClose(1000)

            # if in streaming
            elif 'results' in jsonObject:
                jsonObject = json.loads(payload.decode('utf8'))
                hypothesis = ""
                # empty hypothesis
                if len(jsonObject['results']) != 0:
                    # print("empty hypothesis!")
                # regular hypothesis
                # else:
                    # dump the message to the output directory
                    jsonObject = json.loads(payload.decode('utf8'))
                    with open(self.fileJson, "a") as f:
                        f.write(json.dumps(jsonObject, indent=4,
                                           sort_keys=True))

                    res = jsonObject['results'][0]
                    hypothesis = res['alternatives'][0]['transcript']
                    bFinal = (res['final'] is True)
                    if bFinal:
                        # print('final hypothesis: "' + hypothesis + '"')
                        self.summary[self.uttNumber]['hypothesis'] += hypothesis
                    # else:
                    #     print('interim hyp: "' + hypothesis + '"')

    def onClose(self, wasClean, code, reason):

        # print("onClose")
        # print("WebSocket connection closed: {0}, code: {1}, clean: {2}, "
        #       "reason: {0}".format(reason, code, wasClean))
        self.summary[self.uttNumber]['status']['code'] = code
        self.summary[self.uttNumber]['status']['reason'] = reason

        # create a new WebSocket connection if there are still
        # utterances in the queue that need to be processed
        self.queue.task_done()

        if not self.factory.prepareUtterance():
            return

        # SSL client context: default
        if self.factory.isSecure:
            contextFactory = ssl.ClientContextFactory()
        else:
            contextFactory = None
        connectWS(self.factory, contextFactory)


if __name__ == '__main__':

    with open('recordings.txt', 'w') as f:
        rootdir = os.getcwd() + '/recordings/'
        for subdir, dirs, files in os.walk(rootdir):
            subdirName = subdir[subdir.rfind('/')+1:]
            # print "hi"
            # print subdirName
            for file in files:
                # print "yo"
                # print file
                f.write('./recordings/' + subdirName + '/' + file)
                f.write('\n')

    args = {'contentType':'audio/wav', 'credentials':['45189e29-8725-4df0-8f94-0599fb8564e9', '1FlfCoi0F7tt'], 'dirOutput':'./output', 'fileInput':'./recordings.txt', 'model':'en-GB_NarrowbandModel', 'optOut':False, 'threads':10, 'tokenauth':False}

    

    # create output directory if necessary
    # if os.path.isdir(args['dirOutput']):
    #     fmt = 'the output directory "{}" already exists, overwrite? (y/n)? '
    #     while True:
    #         answer = raw_input(fmt.format(args['dirOutput'])).strip().lower()
    #         if answer == "n":
    #             sys.stderr.write("exiting...")
    #             sys.exit()
    #         elif answer == "y":
    #             break
    # else:
    #     os.makedirs(args['dirOutput'])

    if not os.path.isdir(args['dirOutput']):
        os.makedirs(args['dirOutput'])


    # logging
    log.startLogging(sys.stdout)

    # add audio files to the processing queue
    q = Queue.Queue()
    lines = [line.rstrip('\n') for line in open(args['fileInput'])]
    fileNumber = 0
    finalDictionary = {'0001': [], '0002': [], '0003': [], '0004': []}
    myDictionary = {}
    # finalDictionary = {}
    for fileName in lines:
        # print(fileName)
        exceptFileName = fileName[:fileName.rfind('/')]
        # print "start"
        # print exceptFileName
        dirName = exceptFileName[exceptFileName.rfind('/')+1:]
        # myDictionary[dirName] = myDictionary[dirName].append(fileNumber)
        # print dirName
        # print fileName
        myDictionary[fileNumber] = dirName
        q.put((fileNumber, fileName))
        fileNumber += 1

    hostname = "stream.watsonplatform.net"
    headers = {'X-WDC-PL-OPT-OUT': '1'} if args['optOut'] else {}

    # authentication header
    if args['tokenauth']:
        headers['X-Watson-Authorization-Token'] = (
            Utils.getAuthenticationToken('https://' + hostname,
                                         'speech-to-text',
                                         '45189e29-8725-4df0-8f94-0599fb8564e9',
                                         '1FlfCoi0F7tt'))
    else:
        auth = args['credentials'][0] + ":" + args['credentials'][1]
        data_bytes = auth.encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(data_bytes).decode('ascii')

    # print(headers)
    # create a WS server factory with our protocol
    fmt = "wss://{}/speech-to-text/api/v1/recognize?model={}"
    url = fmt.format(hostname, args['model'])
    summary = {}
    factory = WSInterfaceFactory(q, summary, args['dirOutput'], args['contentType'],
                                 args['model'], url, headers, debug=False)
    factory.protocol = WSInterfaceProtocol

    for i in range(min(int(args['threads']), q.qsize())):

        factory.prepareUtterance()

        # SSL client context: default
        if factory.isSecure:
            contextFactory = ssl.ClientContextFactory()
        else:
            contextFactory = None
        connectWS(factory, contextFactory)

    reactor.run()

    # dump the hypotheses to the output file
    fileHypotheses = args['dirOutput'] + "/hypotheses.txt"
    f = open(fileHypotheses, "w")
    successful = 0
    emptyHypotheses = 0
    # print sorted(summary.items())
    counter = 0
    for key, value in enumerate(sorted(summary.items())):
        value = value[1]  
        if value['status']['code'] == 1000:
            # print('{}: {} {}'.format(key, value['status']['code'],
            #                          value['hypothesis'].encode('utf-8')))
            # print "I am a key "
            direc = myDictionary[key] 
            # print value['hypothesis']
            finalDictionary[direc].append(value['hypothesis'].replace("'", ""))
            # print(fileName)
            # print "YELLOW"
            successful += 1
            if value['hypothesis'][0] == "":
                emptyHypotheses += 1
        else:
            fmt = '{}: {status[code]} REASON: {status[reason]}'
            # print(fmt.format(key, **status))
        f.write('{}: {}\n'.format(counter + 1, value['hypothesis'].encode('utf-8')))
        counter += 1
    f.close()
    fmt = "successful sessions: {} ({} errors) ({} empty hypotheses)"
    # print(fmt.format(successful, len(summary) - successful, emptyHypotheses))
    # print "YAYYYY"
    print (finalDictionary)