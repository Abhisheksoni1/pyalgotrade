# PyAlgoTrade
#
# Copyright 2011-2015 Gabriel Martin Becedillas Ruiz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
.. moduleauthor:: Gabriel Martin Becedillas Ruiz <gabriel.becedillas@gmail.com>
"""

import threading
import Queue
import time

from pyalgotrade import observer
from pyalgotrade import dispatchprio
import pyalgotrade.logger
from pyalgotrade.coinbase import httpclient
from pyalgotrade.coinbase import wsclient
from pyalgotrade.coinbase import obooksync
from pyalgotrade.coinbase import common


logger = pyalgotrade.logger.getLogger(__name__)


REST_API_URL = "https://api.gdax.com"
WEBSOCKET_FEED_URL = "wss://ws-feed.gdax.com"
# https://public.sandbox.exchange.coinbase.com/
SANDBOX_REST_API_URL = "https://api-public.sandbox.gdax.com"
SANDBOX_WEBSOCKET_FEED_URL = "wss://ws-feed-public.sandbox.gdax.com"


def pricelevels_to_obooklevels(priceLevels, maxValues):
    return map(
        lambda level: OrderBookLevel(level.getPrice(), level.getSize()),
        priceLevels.getValues(maxValues=maxValues)
    )


class OrderBookLevel(object):
    """An order book level."""

    def __init__(self, price, size):
        self.__price = price
        self.__size = size

    def getPrice(self):
        """Returns the price."""
        return float(self.__price)

    def getSize(self):
        """Returns the size."""
        return float(self.__size)


class L3OrderBook(object):
    """
    The order book.
    """

    def __init__(self, orderBookSync):
        self.__orderBookSync = orderBookSync

    def getSequence(self):
        return self.__orderBookSync.getSequence()

    def getBids(self, maxValues=20):
        """
        Returns the bids.

        :param maxValues: The maximum number of bids to return.
        :rtype: List of :class:`pyalgotrade.coinbase.client.OrderBookLevel` instances.
        """
        return pricelevels_to_obooklevels(self.__orderBookSync.getBids(), maxValues)

    def getAsks(self, maxValues=20):
        """
        Returns the asks.

        :param maxValues: The maximum number of asks to return.
        :rtype: List of :class:`pyalgotrade.coinbase.client.OrderBookLevel` instances.
        """
        return pricelevels_to_obooklevels(self.__orderBookSync.getAsks(), maxValues)


class WebSocketClient(wsclient.WebSocketClient):
    class Event:
        ERROR = 1
        CONNECTED = 2
        DISCONNECTED = 3
        ORDER_MATCH = 4
        ORDER_RECEIVED = 5
        ORDER_OPEN = 6
        ORDER_DONE = 7
        ORDER_CHANGE = 8
        SEQ_NR_MISMATCH = 9

    def __init__(self, productId, url, maxInactivity=120):
        """
        This class is responsible for forwarding websocket messages through a queue.
        """

        super(WebSocketClient, self).__init__(productId, url, maxInactivity=maxInactivity)
        self.__queue = Queue.Queue()

    def getQueue(self):
        return self.__queue

    def onOpened(self):
        super(WebSocketClient, self).onOpened()
        logger.info("Connection opened.")
        self.__queue.put((WebSocketClient.Event.CONNECTED, None))

    def onConnectionRefused(self, code, reason):
        logger.error("Connection refused. Code: %s. Reason: %s." % (code, reason))

    def onClosed(self, code, reason):
        logger.info("Closed. Code: %s. Reason: %s." % (code, reason))
        self.__queue.put((WebSocketClient.Event.DISCONNECTED, None))

    def onDisconnectionDetected(self):
        logger.warning("Disconnection detected.")
        self.stopClient()
        self.__queue.put((WebSocketClient.Event.DISCONNECTED, None))

    ######################################################################
    # Coinbase specific

    def onError(self, msg):
        logger.error(msg.getMessage())
        self.__queue.put((WebSocketClient.Event.ERROR, msg))

    def onUnknownMessage(self, msgDict):
        logger.warning("Unknown message %s" % msgDict)

    def onSubscriptions(self, msg):
        logger.info("Subscriptions: %s" % msg.getChannels())

    def onSequenceMismatch(self, lastValidSequence, currentSequence):
        logger.warning("Sequence jumped from %s to %s" % (lastValidSequence, currentSequence))
        self.__queue.put((WebSocketClient.Event.SEQ_NR_MISMATCH, currentSequence))

    def onOrderReceived(self, msg):
        self.__queue.put((WebSocketClient.Event.ORDER_RECEIVED, msg))

    def onOrderOpen(self, msg):
        self.__queue.put((WebSocketClient.Event.ORDER_OPEN, msg))

    def onOrderDone(self, msg):
        self.__queue.put((WebSocketClient.Event.ORDER_DONE, msg))

    def onOrderMatch(self, msg):
        self.__queue.put((WebSocketClient.Event.ORDER_MATCH, msg))

    def onOrderChange(self, msg):
        self.__queue.put((WebSocketClient.Event.ORDER_CHANGE, msg))


class WebSocketClientThread(threading.Thread):
    def __init__(self, productId, url, maxInactivity=120):
        """
        Thread class responsible for running a WebSocketClient.
        """

        super(WebSocketClientThread, self).__init__()
        self.__wsClient = WebSocketClient(productId, url, maxInactivity=maxInactivity)
        self.__runEvent = threading.Event()

    def waitRunning(self, timeout):
        return self.__runEvent.wait(timeout)

    def isConnected(self):
        return self.__wsClient.isConnected()

    def getQueue(self):
        return self.__wsClient.getQueue()

    def run(self):
        self.__runEvent.set()

        try:
            logger.info("Connecting websocket client.")
            self.__wsClient.connect()
            logger.info("Running websocket client.")
            self.__wsClient.startClient()
        except Exception, e:
            logger.exception("Failed to run websocket client: %s" % e)

    def stop(self):
        try:
            logger.info("Stopping websocket client.")
            self.__wsClient.stopClient()
        except Exception, e:
            logger.error("Error stopping websocket client: %s." % (str(e)))


class Client(observer.Subject):

    """
    Interface with Coinbase exchange.

    :param productId: The id of the product to trade.
    :param wsURL: Websocket feed url.
    :param apiURL: Rest API url.
    """

    QUEUE_TIMEOUT = 0.01
    WAIT_CONNECT_POLL_FREQUENCY = 0.5
    ORDER_BOOK_EVENT_DISPATCH = {
        WebSocketClient.Event.ORDER_MATCH: obooksync.L3OrderBookSync.onOrderMatch,
        WebSocketClient.Event.ORDER_RECEIVED: obooksync.L3OrderBookSync.onOrderReceived,
        WebSocketClient.Event.ORDER_OPEN: obooksync.L3OrderBookSync.onOrderOpen,
        WebSocketClient.Event.ORDER_DONE: obooksync.L3OrderBookSync.onOrderDone,
        WebSocketClient.Event.ORDER_CHANGE: obooksync.L3OrderBookSync.onOrderChange,
    }

    def __init__(self, productId, wsURL=WEBSOCKET_FEED_URL, apiURL=REST_API_URL):
        self.__productId = productId
        self.__stopped = False
        self.__httpClient = httpclient.HTTPClient(apiURL)
        self.__orderEvents = observer.Event()
        self.__l3OrderBookEvents = observer.Event()
        self.__wsClientThread = None
        self.__lastSeqNr = 0
        self.__l3OBookSync = None
        self.__wsURL = wsURL

    def __connectWS(self, retry):
        while True:
            # Start the client thread and wait a couple of seconds seconds until it starts running
            self.__wsClientThread = WebSocketClientThread(self.__productId, self.__wsURL)
            self.__wsClientThread.start()
            self.__wsClientThread.waitRunning(5)

            # While the thread is alive, wait until it gets connected.
            while self.__wsClientThread.is_alive() and not self.__wsClientThread.isConnected():
                time.sleep(Client.WAIT_CONNECT_POLL_FREQUENCY)

            # Check if the thread is not connected and we should retry.
            if self.__wsClientThread.isConnected() or not retry:
                break

    def refreshOrderBook(self):
        logger.info("Retrieving level 3 order book...")
        obook = self.__httpClient.getOrderBook(product=self.__productId, level=3)
        self.__l3OBookSync = obooksync.L3OrderBookSync(obook)
        logger.info("Finished retrieving level 3 order book")

    def __onConnected(self):
        self.refreshOrderBook()

    def __onDisconnected(self):
        logger.info("Waiting for websocket client to finish.")
        self.__wsClientThread.join()
        logger.info("Done")
        if not self.__stopped:
            self.__connectWS(True)

    def __onSeqNrMismatch(self):
        self.refreshOrderBook()

    def __onOrderEvent(self, eventType, eventData):
        self.__orderEvents.emit(eventData)

        # Update order book
        method = Client.ORDER_BOOK_EVENT_DISPATCH.get(eventType)
        assert method is not None
        updated = method(self.__l3OBookSync, eventData)
        # Emit an event if the orderbook got updated.
        if updated:
            self.__l3OrderBookEvents.emit(L3OrderBook(self.__l3OBookSync))

    def getHTTPClient(self):
        return self.__httpClient

    def getOrderEvents(self):
        return self.__orderEvents

    def getL3OrderBookEvents(self):
        """
        Returns the event that will be emitted when the L3 orderbook gets updated.

        Eventh handlers should receive one parameter:
         1. A :class:`pyalgotrade.coinbase.client.L3OrderBook` instance.

        :rtype: :class:`pyalgotrade.observer.Event`.
        """
        return self.__l3OrderBookEvents

    def start(self):
        self.__connectWS(False)
        if not self.__wsClientThread.isConnected():
            raise Exception("Failed to connect websocket client")

    def stop(self):
        try:
            if self.__wsClientThread is not None:
                self.__stopped = True
                self.__wsClientThread.stop()
        except Exception:
            logger.exception("Error stopping client thread")

    def join(self):
        if self.__wsClientThread is not None:
            self.__wsClientThread.join()

    def eof(self):
        return self.__stopped

    def dispatch(self):
        ret = False

        try:
            eventType, eventData = self.__wsClientThread.getQueue().get(True, Client.QUEUE_TIMEOUT)

            ret = True
            if eventType == WebSocketClient.Event.CONNECTED:
                self.__onConnected()
            elif eventType == WebSocketClient.Event.DISCONNECTED:
                self.__onDisconnected()
            elif eventType in [
                WebSocketClient.Event.ORDER_MATCH,
                WebSocketClient.Event.ORDER_RECEIVED,
                WebSocketClient.Event.ORDER_OPEN,
                WebSocketClient.Event.ORDER_DONE,
                WebSocketClient.Event.ORDER_CHANGE
            ]:
                self.__onOrderEvent(eventType, eventData)
            elif eventType == WebSocketClient.Event.SEQ_NR_MISMATCH:
                self.__onSeqNrMismatch()
            else:
                logger.error("Invalid event received to dispatch: %s - %s" % (eventType, eventData))
                ret = False
        except Queue.Empty:
            pass

        return ret

    def peekDateTime(self):
        return None

    def getDispatchPriority(self):
        # Dispatch events before the broker and barfeed.
        return min(dispatchprio.BROKER, dispatchprio.BAR_FEED) - 1
