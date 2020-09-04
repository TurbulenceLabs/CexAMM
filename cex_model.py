import os
import sys
import hmac
import time
import hashlib
import requests
from queue import Queue
from retrying import retry
from datetime import datetime


def retry_if_not_interrupt(exception):
    return not isinstance(exception, KeyboardInterrupt)


class AMM_Model(object):

    def __init__(self, api_key, secret_key) -> None:
        super().__init__()

        # account Information
        self.api_key = api_key
        self.secret_key = secret_key

        # headers
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-BH-APIKEY': self.api_key,
        }

        # host & urls
        host = 'https://api.hbtc.com/openapi'
        self.urls = {
            'account': os.path.join(host, 'v1/account'),
            'brokerInfo': os.path.join(host, 'v1/brokerInfo'),
            'bookTicker': os.path.join(host, 'quote/v1/ticker/bookTicker'),
            'depth': os.path.join(host, 'quote/v1/depth'),
            'historyOrders': os.path.join(host, 'v1/historyOrders'),
            'order': os.path.join(host, 'v1/order'),
            'openOrders': os.path.join(host, 'v1/openOrders'),
            'price': os.path.join(host, 'quote/v1/ticker/price'),
            'withdrawalOrders': os.path.join(host, 'v1/withdrawalOrders'),
        }

    @retry(retry_on_exception=retry_if_not_interrupt)
    def _hbtc_delete_func(self, url, headers={}, params={}):
        req = requests.delete(url, headers=headers, params=params).json()
        return req

    @retry(retry_on_exception=retry_if_not_interrupt)
    def _hbtc_get_func(self, url, headers={}, params={}):
        req = requests.get(url, headers=headers, params=params).json()
        return req

    @retry(retry_on_exception=retry_if_not_interrupt)
    def _hbtc_post_func(self, url, headers={}, params={}):
        # avoid error code -1121
        req = requests.post(url, headers=headers, params=params).json()
        return req

    def _get_signature_sha256(self, params: dict):
        data = [f'{item}={str(params[item])}' for item in params.keys()]
        signature = hmac.new(self.secret_key.encode('UTF8'), ('&'.join(data)).encode('UTF8'),
                             digestmod=hashlib.sha256).hexdigest()
        return signature

    def _get_order_depth(self):
        params = {'symbol': self.symbol}
        depth = self._hbtc_get_func(self.urls['depth'], params=params)
        return depth

    def _query_broker(self, symbol_name):
        symbols = self._hbtc_get_func(self.urls['brokerInfo'], params={'type': 'token'})['symbols']
        symbol = dict()
        for item in symbols:
            if item['symbol'] == symbol_name:
                symbol['minPrice'] = float(item['filters'][0]['minPrice'])
                symbol['maxPrice'] = float(item['filters'][0]['maxPrice'])
                symbol['pricePrecision'] = len(item['filters'][0]['tickSize'].split('.')[1])
                symbol['minQty'] = float(item['filters'][1]['minQty'])
                symbol['maxQty'] = float(item['filters'][1]['maxQty'])
                symbol['quantityPrecision'] = len(item['filters'][1]['stepSize'].split('.')[1])
                return symbol

    def _order_temp(self, symbol, side, price, quantity):
        params = {
            'symbol': symbol,
            'side': side,
            'type': 'LIMIT',
            'price': round(price, self.symbol_info['pricePrecision']),
            'quantity': round(quantity, self.symbol_info['quantityPrecision']),
            'timeInForce': 'GTC',
            'timestamp': self.timestamp,
        }
        params['signature'] = self._get_signature_sha256(params)
        req = self._hbtc_post_func(self.urls['order'], self.headers, params)
        return req

    def _get_params(self, ps={}):
        params = {'timestamp': self.timestamp}
        for key, value in ps.items():
            params[key] = value
        params['signature'] = self._get_signature_sha256(params)
        return params

    def _show_order(self, orders):
        for order in orders:
            print(order)

    def _make_order(self, orders):
        while orders.qsize():
            info = orders.get()
            req = self._order_temp(symbol=info[0], side=info[1], price=info[2], quantity=info[3])
            print(req)

    def _get_steps(self, num, step) -> list:
        """
        During step 1, make order as a certain step
        :param num: total amounts need to be balance
        :param step: int type
        :return: a list of sub-money
        """
        a, b = divmod(num, step)
        c = [round(step, self.symbol_info['quantityPrecision']) for _ in range(int(a))]
        b = round(b, self.symbol_info['quantityPrecision'])
        if b > self.symbol_info['minQty']:
            c += [b if b <= self.symbol_info['maxQty'] else self.symbol_info['maxQty']]
        return c

    @property
    def timestamp(self):
        return str(int(time.time() * 1000))  # millisecond -> microsecond

    @property
    def now(self):
        return datetime.now().strftime('%H%M%S')

    def query_history_order(self):
        orders = self._hbtc_get_func(self.urls['historyOrders'], self.headers, self._get_params())
        self._show_order(orders)

    def query_withdraw_orders(self):
        orders = self._hbtc_get_func(self.urls['withdrawalOrders'], self.headers, self._get_params())
        self._show_order(orders)

    def query_now_orders(self):
        orders = self._hbtc_get_func(self.urls['openOrders'], self.headers, self._get_params())
        return orders

    def delete_orders(self, orders):
        for order in orders:
            ps = {'orderId': order['orderId']}
            req = self._hbtc_delete_func(self.urls['order'], self.headers, self._get_params(ps))
            print(req)
