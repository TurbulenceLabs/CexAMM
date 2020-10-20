import sys
import time
from queue import Queue

from cex_model import AMM_Model


class Bivar(AMM_Model):
    def __init__(self, api_key, secret_key,
                 shares,
                 first_step=0.01, second_step=0.0,
                 second_order_depth=5,
                 symbol_name='GRIN',
                 ) -> None:
        super().__init__(api_key, secret_key)

        self.symbol_name = self._check_token(symbol_name)
        self.symbol = self._check_pair(symbol_name + 'USDT')
        self.symbol_info = self._query_broker(self.symbol)

        self.account = self.check_account()
        if len(self.account) != 2:
            self.print_error_message('This procedure only support 2 assets')

        self.ratio = 0.0
        self.shares = self._normalize_shares(shares)
        self.ratio_ab = self.shares[self.symbol_name] / self.shares['USDT']
        self.order_book_queue = Queue()  # (symbol, side, price, quantity)

        self.first_step = first_step
        self.second_step = second_step

        self.second_order_depth = second_order_depth
        self._second_lambda_build()
        self.second_orders = list()  # symbol, side, price, quantity
        self.second_total_orders = 0

        self.second_fresh_base()

    @property
    def total_assets(self):
        return sum([float(p['total_usdt_price']) for p in self.account])

    @property
    def free_assets(self):
        return sum([float(p['free_usdt_price']) for p in self.account])

    @property
    def locked_assets(self):
        return sum([float(p['locked_usdt_price']) for p in self.account])

    def check_account(self):
        assets = [dict() for _ in range(2)]
        for item in self._hbtc_get_func(self.urls['account'], self.headers, self._get_params())['balances']:
            if item['assetName'] == self.symbol_name:
                assets[0] = item
            if item['assetName'] == 'USDT':
                assets[1] = item

        if len(assets[1]) == 1:
            assets[1] = {'asset': 'USDT', 'assetId': 'USDT', 'assetName': 'USDT',
                         'total': '0', 'free': '0', 'locked': '0'}

        for asset in assets:
            asset['total_usdt_price'] = str(float(asset['total']) * self._get_price_usdt(asset['assetName']))
            asset['free_usdt_price'] = str(float(asset['free']) * self._get_price_usdt(asset['assetName']))
            asset['locked_usdt_price'] = str(float(asset['locked']) * self._get_price_usdt(asset['assetName']))
        return assets

    def update_ratio(self):
        self.account = self.check_account()
        ratio = float(self.account[0]['total_usdt_price']) / float(self.account[1]['total_usdt_price'])
        return ratio

    def _get_book_price_usdt(self, symbol: str):
        book_price_info = {'bidPrice': 1.0, 'askPrice': 1.0}
        symbol = symbol.upper()
        if symbol != 'USDT':
            book_price_info = self._hbtc_get_func(self.urls['bookTicker'], params={'symbol': f'{symbol}USDT'})
        return book_price_info

    def _get_price_usdt(self, symbol: str):
        return self._get_price(f'{symbol}USDT')

    def is_best_price(self, order):
        order_price, order_quantity = float(order['price']), float(order['origQty'])

        order_book = self._get_order_depth()
        if order['side'] == 'BUY':
            new_price, quantity = float(order_book['bids'][0][0]), float(order_book['bids'][0][1])
        else:
            new_price, quantity = float(order_book['asks'][0][0]), float(order_book['asks'][0][1])

        return (new_price == order_price) and (order_quantity != quantity)

    def first_balance_symbol2usdt(self):
        side = 'BUY' if self.ratio < self.ratio_ab else 'SELL'

        order_step = self.total_assets * self.first_step
        symbol_book_info = self._get_book_price_usdt(self.account[0]['assetName'])
        symbol_quantity, usdt = float(self.account[0]['total']), float(self.account[1]['total'])

        price = float(symbol_book_info['bidPrice']) if side == 'BUY' else float(symbol_book_info['askPrice'])
        delta_qty = abs((self.ratio_ab * usdt - price * symbol_quantity) / (price * (1 + self.ratio_ab)))

        operation_assets = self._get_steps(price * delta_qty, order_step)[0:1]
        for item in operation_assets:
            self.order_book_queue.put((self.symbol, side, price, item / price))

        self.print_log_message(f'Step 1: ${self.total_assets}, ratio: {self.ratio}, order: {(len(operation_assets))}')
        self._make_order(self.order_book_queue)

    def _second_delete_orders(self, price_idxes):
        orders = list()
        _, prices, _ = self._second_price_idx2info(price_idxes)
        for history_order in self.query_now_orders():
            for price in prices:
                if float(history_order['price']) == float(round(price, self.symbol_info['pricePrecision'])):
                    orders.append(history_order)
                    break
        self.delete_orders(orders)

    def _second_lambda_build(self):
        # bp: base price, bq: base_quantity
        self.new_price = lambda bp, j, step: round(bp * pow(1 + step, j), self.symbol_info['pricePrecision'])
        # rate: a token / (a token + USDT)
        # self.delta_qty = lambda bq, step, rate, j: round(
        #     bq * pow(1 + step * rate, j - 1) * step * rate * (1 - rate) / pow(1 + step, j),
        #     self.symbol_info['quantityPrecision'])
        self.delta_qty = lambda bq, step, ratio_ab, j: round(abs(pow(1 + step, j) - 1) * bq / (1 + ratio_ab),
                                                             self.symbol_info['quantityPrecision'])

    def _second_make_orders(self, price_idxes):
        sides, prices, delta_qties = self._second_price_idx2info(price_idxes)
        for idx1, idx2 in ([[i, -i - 1] for i in range(len(sides) // 2)][::-1]):
            self.order_book_queue.put((self.symbol, sides[idx1], prices[idx1], delta_qties[idx1]))
            self.order_book_queue.put((self.symbol, sides[idx2], prices[idx2], delta_qties[idx2]))
        self._make_order(self.order_book_queue)

    def _second_price_idx2info(self, price_idxes):
        sides, prices, delta_qtys = list(), list(), list()
        for price_idx in price_idxes:
            side = 'SELL' if price_idx > (sum(self.second_idx_list) / len(self.second_idx_list)) else 'BUY'
            price = self.new_price(self.second_base_price, price_idx, self.second_step)
            # delta_qty = self.delta_qty(self.second_base_qty, self.second_step, self.ratio_ab / (1 + self.ratio_ab),
            #                            price_idx)
            sides.append(side)
            prices.append(price)
            delta_qtys.append(delta_qty)
        return sides, prices, delta_qtys

    def second_fresh_base(self):
        self.second_total_orders = 0
        order_book = self._get_order_depth()

        self.second_base_price = (float(order_book['bids'][0][0]) + float(order_book['asks'][0][0])) * 0.5
        self.second_base_qty = float(self.account[0]['total'])

        self.second_idx_list = list(range(self.second_order_depth, 0, -1)) + list(
            range(-1, -self.second_order_depth - 1, -1))

    def second_get_now_order_idxes(self):
        history_orders = self.query_now_orders()
        if len(history_orders) == len(self.second_idx_list):
            return list()
        history_prices = sorted([float(order['price']) for order in history_orders])

        complete_order_idxes = list()
        for idx, prc_idx in enumerate(self.second_idx_list):
            flag = True
            for his_prc in history_prices:
                _tem = round(self.new_price(self.second_base_price, prc_idx, self.second_step),
                             self.symbol_info['pricePrecision'])
                if his_prc == _tem:
                    flag = False
                    break
            if flag:
                complete_order_idxes.append(prc_idx)

        return complete_order_idxes

    def second_fresh_idx_list(self, complete_order_idxes: list):
        if len(complete_order_idxes) == 0:
            return
        elif len(complete_order_idxes) == 2 * self.second_order_depth:
            new_order_idxes = self.second_idx_list
            delete_order_idxes = list()
        else:
            if sum(complete_order_idxes) > (sum(self.second_idx_list) / len(self.second_idx_list)):
                standard_index = complete_order_idxes[0]
                order_idxes = list(range(standard_index + self.second_order_depth, standard_index, -1))
                order_idxes += list(range(standard_index - 1, standard_index - 1 - self.second_order_depth, -1))
            elif sum(complete_order_idxes) < (sum(self.second_idx_list) / len(self.second_idx_list)):
                standard_index = complete_order_idxes[-1]
                order_idxes = list(range(standard_index + self.second_order_depth, standard_index, -1))
                order_idxes += list(range(standard_index - 1, standard_index - self.second_order_depth - 1, -1))
            else:
                order_idxes = self.second_idx_list
            cur_orders = set(self.second_idx_list) - set(complete_order_idxes)
            new_order_idxes = sorted(list(set(order_idxes) - cur_orders), reverse=True)
            delete_order_idxes = sorted(list(cur_orders - set(order_idxes)), reverse=True)
            self.second_idx_list = order_idxes
        self.print_log_message(f'Step 2: ${self.total_assets}, ratio: {self.ratio} order: {len(self.second_idx_list)}')
        self._second_delete_orders(delete_order_idxes)
        self._second_make_orders(new_order_idxes)
        self.second_total_orders += len(new_order_idxes)


if __name__ == '__main__':
    # params
    api_key, secret_key = sys.argv[1], sys.argv[2]
    # ------------------------- Manual Parameters ---------------------------
    # token information
    symbol_name = 'GRIN'
    shares = {'GRIN': 7, 'USDT': 3}

    # hyper parameters
    balance_ratio_condition = 0.20
    first_step, second_step = 0.005, 0.01  # total asset as USDT ratio
    second_order_depth = 5
    second_total_orders_threshold = 100
    second_restart_time = '03000'
    # -----------------------------------------------------------------------

    # Initiate monitor
    bivar = Bivar(api_key=api_key, secret_key=secret_key,
                  shares=shares,
                  first_step=first_step, second_step=second_step,
                  second_order_depth=second_order_depth,
                  symbol_name=symbol_name,
                  )
    bivar.print_info_message(f'CexAMM is standing by!!!!!!!!')

    # AMM condition
    if bivar.total_assets < 500:
        bivar.print_error_message("Charge some USDT! BABY!!")

    bivar.ratio = bivar.update_ratio()
    orders = bivar.query_now_orders()
    bivar.print_log_message(f'CexAMM is completed! ${bivar.total_assets}, ratio: {bivar.ratio}, order: {len(orders)}')

    # restart step2
    if abs(bivar.ratio_ab - bivar.ratio) < balance_ratio_condition:
        bivar.delete_orders(orders)

    # Main Procedure
    while True:
        # Condition 1
        if abs(bivar.ratio_ab - bivar.ratio) >= balance_ratio_condition:
            if len(orders) == 0:
                bivar.first_balance_symbol2usdt()
            elif len(orders) > 1 or (not bivar.is_best_price(orders[0])):
                bivar.delete_orders(orders)
                bivar.first_balance_symbol2usdt()

        # Condition 2
        else:
            if (len(orders) <= 1) or (bivar.now[:5] == second_restart_time) or (
                    bivar.second_total_orders >= second_total_orders_threshold):
                if bivar.now[:5] == second_restart_time:
                    time.sleep(10)
                bivar.delete_orders(orders)
                bivar.second_fresh_base()
                idxes = bivar.second_idx_list
            else:
                idxes = bivar.second_get_now_order_idxes()
            bivar.second_fresh_idx_list(idxes)

        # restart information
        time.sleep(1)
        bivar.ratio = bivar.update_ratio()
        orders = bivar.query_now_orders()
