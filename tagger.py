#!/usr/bin/env python3

# This script takes Amazon "Order History Reports" and annotates your Mint
# transactions based on actual items in each purchase. It can handle orders
# that are split into multiple shipments/charges, and can even itemized each
# transaction for maximal control over categorization.

# First, you must generate and download your order history reports from:
# https://www.amazon.com/gp/b2b/reports

import argparse
import atexit
from collections import defaultdict, Counter
import datetime
from dotenv import load_dotenv, find_dotenv
import itertools
import logging
import os
import pickle
import pkg_resources
import time
from threading import Thread

import getpass
import keyring
from mintapi.api import Mint, MINT_ROOT_URL
from progress.bar import IncrementalBar
from progress.counter import Counter as ProgressCounter
from progress.spinner import Spinner
import readchar

import amazon
import category
from currency import micro_usd_nearly_equal
from currency import micro_usd_to_usd_float
from currency import micro_usd_to_usd_string
import mint


load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


KEYRING_SERVICE_NAME = 'mintapi'

UPDATE_TRANS_ENDPOINT = '/updateTransaction.xevent'


class AsyncProgress:
    def __init__(self, progress):
        super()
        self.progress = progress
        self.spinning = True
        self.timer = Thread(target=self.runnable)
        self.timer.start()

    def runnable(self):
        while self.spinning:
            self.progress.next()
            time.sleep(0.1)

    def finish(self):
        self.spinning = False
        self.progress.finish()
        print()


def main():
    if float(pkg_resources.get_distribution('mintapi').version) < 1.29:
        print('You are running an incompatible version of mintapi! Please: \n'
              '  python3 -m pip -U mintapi')
        exit(1)

    parser = argparse.ArgumentParser(
        description='Tag Mint transactions based on itemized Amazon history.')
    define_args(parser)
    args = parser.parse_args()

    if args.dry_run:
        logger.info('\nDry Run; no modifications being sent to Mint.\n')

    # Initialize the stats. Explicitly initialize stats that might not be
    # accumulated (conditionals).
    stats = Counter(
        adjust_itemized_tax=0,
        already_up_to_date=0,
        misc_charge=0,
        new_tag=0,
        no_retag=0,
        retag=0,
        user_skipped_retag=0,
        personal_cat=0,
    )

    orders = amazon.Order.parse_from_csv(
        args.orders_csv, ProgressCounter('Parsing Orders - '))
    items = amazon.Item.parse_from_csv(
        args.items_csv, ProgressCounter('Parsing Items - '))
    refunds = ([] if not args.refunds_csv
               else amazon.Refund.parse_from_csv(
                   args.refunds_csv, ProgressCounter('Parsing Refunds - ')))

    mint_client = None

    def close_mint_client():
        if mint_client:
            mint_client.close()

    atexit.register(close_mint_client)

    if args.pickled_epoch:
        mint_trans, mint_category_name_to_id = (
            get_trans_and_categories_from_pickle(args.pickled_epoch))
    else:
        mint_client = get_mint_client(args)

        # Only get transactions as new as the oldest Amazon order.
        oldest_trans_date = min([o.order_date for o in orders])
        if refunds:
            oldest_trans_date = min(
                oldest_trans_date,
                min([o.order_date for o in refunds]))
        mint_transactions_json, mint_category_name_to_id = (
            get_trans_and_categories_from_mint(mint_client, oldest_trans_date))
        epoch = int(time.time())
        mint_trans = mint.Transaction.parse_from_json(mint_transactions_json)
        dump_trans_and_categories(mint_trans, mint_category_name_to_id, epoch)

    mint_historic_category_renames = get_mint_category_history_for_items(
        mint_trans, args)
    updates, unmatched_orders = get_mint_updates(
        orders, items, refunds,
        mint_trans,
        args, stats,
        mint_historic_category_renames,
        mint_category_name_to_id)

    log_amazon_stats(items, orders, refunds)
    log_processing_stats(stats)

    if args.print_unmatched and unmatched_orders:
        logger.warning(
            'The following were not matched to Mint transactions:\n')
        by_oid = defaultdict(list)
        for uo in unmatched_orders:
            by_oid[uo.order_id].append(uo)
        for orders in by_oid.values():
            if orders[0].is_debit:
                print_unmatched(amazon.Order.merge(orders))
            else:
                for r in amazon.Refund.merge(orders):
                    print_unmatched(r)

    if not updates:
        logger.info(
            'All done; no new tags to be updated at this point in time!')
        exit(0)

    if args.dry_run:
        logger.info('Dry run. Following are proposed changes:')
        if args.skip_dry_print:
            logger.info('Dry run print results skipped!')
        else:
            print_dry_run(updates, ignore_category=args.no_tag_categories)

    else:
        # Ensure we have a Mint client.
        if not mint_client:
            mint_client = get_mint_client(args)

        send_updates_to_mint(
            updates, mint_client, ignore_category=args.no_tag_categories)


def get_mint_category_history_for_items(trans, args):
    """Gets a mapping of item name -> category name.

    For use in memorizing personalized categories.
    """
    if args.do_not_predict_categories:
        return None
    # Don't worry about pending.
    trans = [t for t in trans if not t.is_pending]
    # Only do debits for now.
    trans = [t for t in trans if t.is_debit]

    # Filter for transactions that have been tagged before.
    valid_prefixes = args.amazon_domains.lower().split(',')
    valid_prefixes = ['{}: '.format(pre) for pre in valid_prefixes]
    if args.description_prefix_override:
        valid_prefixes.append(args.description_prefix_override.lower())
    trans = [t for t in trans if
             any(t.merchant.lower().startswith(pre)
                 for pre in valid_prefixes)]

    # Filter out the default category: there is no signal here.
    trans = [t for t in trans
             if t.category != category.DEFAULT_MINT_CATEGORY]

    # Filter out non-item merchants.
    trans = [t for t in trans
             if t.merchant not in mint.NON_ITEM_MERCHANTS]

    item_to_cats = defaultdict(Counter)
    for t in trans:
        # Remove the prefix for the item:
        for pre in valid_prefixes:
            item_name = t.merchant.lower()
            # Find & remove the prefix and remove any leading '3x '.
            if item_name.startswith(pre):
                item_name = amazon.rm_leading_qty(item_name[len(pre):])
                break

        item_to_cats[item_name][t.category] += 1

    item_to_most_common = {}
    for item_name, counter in item_to_cats.items():
        item_to_most_common[item_name] = counter.most_common()[0][0]

    return item_to_most_common


def get_mint_updates(
        orders, items, refunds,
        trans,
        args, stats,
        mint_historic_category_renames=None,
        mint_category_name_to_id=category.DEFAULT_MINT_CATEGORIES_TO_IDS):
    # Remove items from canceled orders.
    items = [i for i in items if not i.is_cancelled()]
    # Remove items that haven't shipped yet (also aren't charged).
    items = [i for i in items if i.order_status == 'Shipped']
    # Remove items with zero quantity (it happens!)
    items = [i for i in items if i.quantity > 0]
    # Make more Items such that every item is quantity 1. This is critical
    # prior to associate_items_with_orders such that items with non-1
    # quantities split into different packages can be associated with the
    # appropriate order.
    items = [si for i in items for si in i.split_by_quantity()]

    itemProgress = IncrementalBar(
        'Matching Amazon Items with Orders',
        max=len(items))
    amazon.associate_items_with_orders(orders, items, itemProgress)
    itemProgress.finish()

    # Only match orders that have items.
    orders = [o for o in orders if o.items]

    trans = mint.Transaction.unsplit(trans)
    stats['trans'] = len(trans)
    # Skip t if the original description doesn't contain 'amazon'
    merch_whitelist = args.mint_input_merchant_filter.lower().split(',')
    trans = [t for t in trans if any(
        merch_str in t.omerchant.lower() for merch_str in merch_whitelist)]
    stats['amazon_in_desc'] = len(trans)
    # Skip t if it's pending.
    trans = [t for t in trans if not t.is_pending]
    stats['pending'] = stats['amazon_in_desc'] - len(trans)
    # Skip t if a category filter is given and t does not match.
    if args.mint_input_categories_filter:
        cat_whitelist = set(
            args.mint_input_categories_filter.lower().split(','))
        trans = [t for t in trans if t.category.lower() in cat_whitelist]

    # Match orders.
    orderMatchProgress = IncrementalBar(
        'Matching Amazon Orders w/ Mint Trans',
        max=len(orders))
    match_transactions(trans, orders, orderMatchProgress)
    orderMatchProgress.finish()

    unmatched_trans = [t for t in trans if not t.orders]

    # Match refunds.
    refundMatchProgress = IncrementalBar(
        'Matching Amazon Refunds w/ Mint Trans',
        max=len(refunds))
    match_transactions(unmatched_trans, refunds, refundMatchProgress)
    refundMatchProgress.finish()

    unmatched_orders = [o for o in orders if not o.matched]
    unmatched_trans = [t for t in trans if not t.orders]
    unmatched_refunds = [r for r in refunds if not r.matched]

    num_gift_card = len([o for o in unmatched_orders
                         if 'Gift Certificate' in o.payment_instrument_type])
    num_unshipped = len([o for o in unmatched_orders if not o.shipment_date])

    matched_orders = [o for o in orders if o.matched]
    matched_trans = [t for t in trans if t.orders]
    matched_refunds = [r for r in refunds if r.matched]

    stats['trans_unmatch'] = len(unmatched_trans)
    stats['order_unmatch'] = len(unmatched_orders)
    stats['refund_unmatch'] = len(unmatched_refunds)
    stats['trans_match'] = len(matched_trans)
    stats['order_match'] = len(matched_orders)
    stats['refund_match'] = len(matched_refunds)
    stats['skipped_orders_gift_card'] = num_gift_card
    stats['skipped_orders_unshipped'] = num_unshipped

    merged_orders = []
    merged_refunds = []

    updateCounter = IncrementalBar('Determining Mint Updates')
    updates = []
    for t in updateCounter.iter(matched_trans):
        if t.is_debit:
            order = amazon.Order.merge(t.orders)
            merged_orders.extend(orders)

            prefix = '{}: '.format(order.website)
            if args.description_prefix_override:
                prefix = args.description_prefix_override

            if order.attribute_subtotal_diff_to_misc_charge():
                stats['misc_charge'] += 1
            # It's nice when "free" shipping cancels out with the shipping
            # promo, even though there is tax on said free shipping. Spread
            # that out across the items instead.
            # if order.attribute_itemized_diff_to_shipping_tax():
            #     stats['add_shipping_tax'] += 1
            if order.attribute_itemized_diff_to_per_item_tax():
                stats['adjust_itemized_tax'] += 1

            assert micro_usd_nearly_equal(t.amount, order.total_charged)
            assert micro_usd_nearly_equal(t.amount, order.total_by_subtotals())
            assert micro_usd_nearly_equal(t.amount, order.total_by_items())

            new_transactions = order.to_mint_transactions(
                t,
                skip_free_shipping=not args.verbose_itemize)

        else:
            refunds = amazon.Refund.merge(t.orders)
            merged_refunds.extend(refunds)
            prefix = '{} refund: '.format(refunds[0].website)

            if args.description_return_prefix_override:
                prefix = args.description_return_prefix_override

            new_transactions = [
                r.to_mint_transaction(t)
                for r in refunds]

        assert micro_usd_nearly_equal(
            t.amount,
            mint.Transaction.sum_amounts(new_transactions))

        for nt in new_transactions:
            # Look if there's a personal category tagged.
            item_name = amazon.rm_leading_qty(nt.merchant.lower())
            if (mint_historic_category_renames and
                    item_name in mint_historic_category_renames):
                suggested_cat = mint_historic_category_renames[item_name]
                if suggested_cat != nt.category:
                    stats['personal_cat'] += 1
                    nt.category = mint_historic_category_renames[item_name]

            nt.update_category_id(mint_category_name_to_id)

        summarize_single_item_order = (
            t.is_debit and len(order.items) == 1 and not args.verbose_itemize)
        if args.no_itemize or summarize_single_item_order:
            new_transactions = mint.summarize_new_trans(
                t, new_transactions, prefix)
        else:
            new_transactions = mint.itemize_new_trans(new_transactions, prefix)

        if mint.Transaction.old_and_new_are_identical(
                t, new_transactions, ignore_category=args.no_tag_categories):
            stats['already_up_to_date'] += 1
            continue

        valid_prefixes = (
            args.amazon_domains.lower().split(',') + [prefix.lower()])
        if any(t.merchant.lower().startswith(pre) for pre in valid_prefixes):
            if args.prompt_retag:
                if args.num_updates > 0 and len(updates) >= args.num_updates:
                    break
                logger.info('\nTransaction already tagged:')
                print_dry_run(
                    [(t, new_transactions)],
                    ignore_category=args.no_tag_categories)
                logger.info('\nUpdate tag to proposed? [Yn] ')
                action = readchar.readchar()
                if action == '':
                    exit(1)
                if action not in ('Y', 'y', '\r', '\n'):
                    stats['user_skipped_retag'] += 1
                    continue
                stats['retag'] += 1
            elif not args.retag_changed:
                stats['no_retag'] += 1
                continue
            else:
                stats['retag'] += 1
        else:
            stats['new_tag'] += 1
        updates.append((t, new_transactions))

    if args.num_updates > 0:
        updates = updates[:args.num_updates]

    return updates, unmatched_orders + unmatched_refunds


def mark_best_as_matched(t, list_of_orders_or_refunds, progress=None):
    if not list_of_orders_or_refunds:
        return

    # Only consider it a match if the posted date (transaction date) is
    # within 3 days of the ship date of the order.
    closest_match = None
    closest_match_num_days = 365  # Large number

    for orders in list_of_orders_or_refunds:
        an_order = next(o for o in orders if o.transact_date())
        if not an_order:
            continue
        num_days = (t.odate - an_order.transact_date()).days
        # TODO: consider orders even if it has a matched_transaction if this
        # transaction is closer.
        already_matched = any([o.matched for o in orders])
        if (abs(num_days) < 4 and
                abs(num_days) < closest_match_num_days and
                not already_matched):
            closest_match = orders
            closest_match_num_days = abs(num_days)

    if closest_match:
        for o in closest_match:
            o.match(t)
        t.match(closest_match)
        if progress:
            progress.next(len(closest_match))


def match_transactions(unmatched_trans, unmatched_orders, progress=None):
    # Also works with Refund objects.
    # First pass: Match up transactions that exactly equal an order's charged
    # amount.
    amount_to_orders = defaultdict(list)

    for o in unmatched_orders:
        amount_to_orders[o.transact_amount()].append([o])

    for t in unmatched_trans:
        mark_best_as_matched(t, amount_to_orders[t.amount], progress)

    unmatched_orders = [o for o in unmatched_orders if not o.matched]
    unmatched_trans = [t for t in unmatched_trans if not t.orders]

    # Second pass: Match up transactions to a combination of orders (sometimes
    # they are charged together).
    oid_to_orders = defaultdict(list)
    for o in unmatched_orders:
        oid_to_orders[o.order_id].append(o)
    amount_to_orders = defaultdict(list)
    for orders_same_id in oid_to_orders.values():
        combos = []
        for r in range(2, len(orders_same_id) + 1):
            combos.extend(itertools.combinations(orders_same_id, r))
        for c in combos:
            orders_total = sum([o.transact_amount() for o in c])
            amount_to_orders[orders_total].append(c)

    for t in unmatched_trans:
        mark_best_as_matched(t, amount_to_orders[t.amount], progress)


def get_mint_client(args):
    email = args.mint_email
    password = args.mint_password

    if not email:
        email = os.getenv('MINT_EMAIL', None)

    if not email:
        email = input('Mint email: ')

    # This was causing my grief. Let's let it rest for a while.
    # if not password:
    #     password = keyring.get_password(KEYRING_SERVICE_NAME, email)

    if not password:
        password = getpass.getpass('Mint password: ')

    if not email or not password:
        logger.error('Missing Mint email or password.')
        exit(1)

    asyncSpin = AsyncProgress(Spinner('Logging into Mint '))

    mint_client = Mint.create(email, password)

    # On success, save off password to keyring.
    keyring.set_password(KEYRING_SERVICE_NAME, email, password)

    asyncSpin.finish()

    return mint_client


MINT_TRANS_PICKLE_FMT = 'Mint {} Transactions.pickle'
MINT_CATS_PICKLE_FMT = 'Mint {} Categories.pickle'


def get_trans_and_categories_from_pickle(pickle_epoch):
    label = 'Un-pickling Mint transactions from epoch: {} '.format(
        pickle_epoch)
    asyncSpin = AsyncProgress(Spinner(label))
    with open(MINT_TRANS_PICKLE_FMT.format(pickle_epoch), 'rb') as f:
        trans = pickle.load(f)
    with open(MINT_CATS_PICKLE_FMT.format(pickle_epoch), 'rb') as f:
        cats = pickle.load(f)
    asyncSpin.finish()

    return trans, cats


def dump_trans_and_categories(trans, cats, pickle_epoch):
    label = 'Backing up Mint to local pickle file, epoch: {} '.format(
        pickle_epoch)
    asyncSpin = AsyncProgress(Spinner(label))
    with open(MINT_TRANS_PICKLE_FMT.format(pickle_epoch), 'wb') as f:
        pickle.dump(trans, f)
    with open(MINT_CATS_PICKLE_FMT.format(pickle_epoch), 'wb') as f:
        pickle.dump(cats, f)
    asyncSpin.finish()


def get_trans_and_categories_from_mint(mint_client, oldest_trans_date):
    # Create a map of Mint category name to category id.
    logger.info('Creating Mint Category Map.')
    start_time = time.time()
    asyncSpin = AsyncProgress(Spinner('Fetching Categories '))
    categories = dict([
        (cat_dict['name'], cat_id)
        for (cat_id, cat_dict) in mint_client.get_categories().items()])
    asyncSpin.finish()

    today = datetime.datetime.now().date()
    # Double the length of transaction history to help aid in
    # personalized category tagging overrides.
    start_date = today - (today - oldest_trans_date) * 2
    start_date_str = start_date.strftime('%m/%d/%y')
    logger.info('Get all Mint transactions since {}.'.format(
        start_date_str))
    asyncSpin = AsyncProgress(Spinner('Fetching Transactions '))
    transactions = mint_client.get_transactions_json(
        start_date=start_date_str,
        include_investment=False,
        skip_duplicates=True)
    asyncSpin.finish()

    dur = s_to_time(time.time() - start_time)
    logger.info('Got {} transactions and {} categories from Mint in {}'.format(
        len(transactions), len(categories), dur))

    return transactions, categories


def log_amazon_stats(items, orders, refunds):
    logger.info('\nAmazon Stats:')
    first_order_date = min([o.order_date for o in orders])
    last_order_date = max([o.order_date for o in orders])
    logger.info('\n{} orders with {} matching items'.format(
        len([o for o in orders if o.items_matched]),
        len([i for i in items if i.matched])))
    logger.info('{} unmatched orders and {} unmatched items'.format(
        len([o for o in orders if not o.items_matched]),
        len([i for i in items if not i.matched])))
    logger.info('Orders ranging from {} to {}'.format(
        first_order_date, last_order_date))

    per_item_totals = [i.item_total for i in items]
    per_order_totals = [o.total_charged for o in orders]

    logger.info('{} total spend'.format(
        micro_usd_to_usd_string(sum(per_order_totals))))

    logger.info('{} avg order total (range: {} - {})'.format(
        micro_usd_to_usd_string(sum(per_order_totals) / len(orders)),
        micro_usd_to_usd_string(min(per_order_totals)),
        micro_usd_to_usd_string(max(per_order_totals))))
    logger.info('{} avg item price (range: {} - {})'.format(
        micro_usd_to_usd_string(sum(per_item_totals) / len(items)),
        micro_usd_to_usd_string(min(per_item_totals)),
        micro_usd_to_usd_string(max(per_item_totals))))

    if refunds:
        first_refund_date = min(
            [r.refund_date for r in refunds if r.refund_date])
        last_refund_date = max(
            [r.refund_date for r in refunds if r.refund_date])
        logger.info('\n{} refunds dating from {} to {}'.format(
            len(refunds), first_refund_date, last_refund_date))

        per_refund_totals = [r.total_refund_amount for r in refunds]

        logger.info('{} total refunded'.format(
            micro_usd_to_usd_string(sum(per_refund_totals))))


def log_processing_stats(stats):
    logger.info(
        '\nTransactions: {trans}\n'
        'Transactions w/ "Amazon" in description: {amazon_in_desc}\n'
        'Transactions ignored: is pending: {pending}\n'
        '\n'
        'Orders matched w/ transactions: {order_match} (unmatched orders: '
        '{order_unmatch})\n'
        'Refunds matched w/ transactions: {refund_match} (unmatched refunds: '
        '{refund_unmatch})\n'
        'Transactions matched w/ orders/refunds: {trans_match} (unmatched: '
        '{trans_unmatch})\n'
        '\n'
        'Orders skipped: not shipped: {skipped_orders_unshipped}\n'
        'Orders skipped: gift card used: {skipped_orders_gift_card}\n'
        '\n'
        'Order fix-up: incorrect tax itemization: {adjust_itemized_tax}\n'
        'Order fix-up: has a misc charges (e.g. gift wrap): {misc_charge}\n'
        '\n'
        'Transactions ignored; already tagged & up to date: '
        '{already_up_to_date}\n'
        'Transactions ignored; ignore retags: {no_retag}\n'
        'Transactions ignored; user skipped retag: {user_skipped_retag}\n'
        '\n'
        'Transactions with personalize categories: {personal_cat}\n'
        '\n'
        'Transactions to be retagged: {retag}\n'
        'Transactions to be newly tagged: {new_tag}\n'.format(**stats))


def print_unmatched(amzn_obj):
    proposed_mint_desc = mint.summarize_title(
        [i.get_title() for i in amzn_obj.items]
        if amzn_obj.is_debit else [amzn_obj.get_title()],
        '{}{}: '.format(
            amzn_obj.website, '' if amzn_obj.is_debit else ' refund'))
    logger.warning('{}'.format(proposed_mint_desc))
    logger.warning('\t{}\t{}\t{}'.format(
        amzn_obj.transact_date()
        if amzn_obj.transact_date()
        else 'Never shipped!',
        micro_usd_to_usd_string(amzn_obj.transact_amount()),
        amazon.get_invoice_url(amzn_obj.order_id)))
    logger.warning('')


def print_dry_run(orig_trans_to_tagged, ignore_category=False):
    for orig_trans, new_trans in orig_trans_to_tagged:
        oid = orig_trans.orders[0].order_id
        logger.info('\nFor Amazon {}: {}\nInvoice URL: {}'.format(
            'Order' if orig_trans.is_debit else 'Refund',
            oid, amazon.get_invoice_url(oid)))

        if orig_trans.children:
            for i, trans in enumerate(orig_trans.children):
                logger.info('{}{}) Current: \t{}'.format(
                    '\n' if i == 0 else '',
                    i + 1,
                    trans.dry_run_str()))
        else:
            logger.info('\nCurrent: \t{}'.format(
                orig_trans.dry_run_str()))

        if len(new_trans) == 1:
            trans = new_trans[0]
            logger.info('\nProposed: \t{}'.format(
                trans.dry_run_str(ignore_category)))
        else:
            for i, trans in enumerate(reversed(new_trans)):
                logger.info('{}{}) Proposed: \t{}'.format(
                    '\n' if i == 0 else '',
                    i + 1,
                    trans.dry_run_str(ignore_category)))


def send_updates_to_mint(updates, mint_client, ignore_category=False):
    updateProgress = IncrementalBar(
        'Updating Mint',
        max=len(updates))

    start_time = time.time()
    num_requests = 0
    for (orig_trans, new_trans) in updates:
        if len(new_trans) == 1:
            # Update the existing transaction.
            trans = new_trans[0]
            modify_trans = {
                'task': 'txnedit',
                'txnId': '{}:0'.format(trans.id),
                'note': trans.note,
                'merchant': trans.merchant,
                'token': mint_client.token,
            }
            if not ignore_category:
                modify_trans = {
                    **modify_trans,
                    'category': trans.category,
                    'catId': trans.category_id,
                }

            logger.debug('Sending a "modify" transaction request: {}'.format(
                modify_trans))
            response = mint_client.post(
                '{}{}'.format(
                    MINT_ROOT_URL,
                    UPDATE_TRANS_ENDPOINT),
                data=modify_trans).text
            updateProgress.next()
            logger.debug('Received response: {}'.format(response))
            num_requests += 1
        else:
            # Split the existing transaction into many.
            # If the existing transaction is a:
            #   - credit: positive amount is credit, negative debit
            #   - debit: positive amount is debit, negative credit
            itemized_split = {
                'txnId': '{}:0'.format(orig_trans.id),
                'task': 'split',
                'data': '',  # Yup this is weird.
                'token': mint_client.token,
            }
            for (i, trans) in enumerate(new_trans):
                amount = trans.amount
                # Based on the comment above, if the original transaction is a
                # credit, flip the amount sign for things to work out!
                if not orig_trans.is_debit:
                    amount *= -1
                amount = micro_usd_to_usd_float(amount)
                itemized_split['amount{}'.format(i)] = amount
                # Yup. Weird:
                itemized_split['percentAmount{}'.format(i)] = amount
                itemized_split['merchant{}'.format(i)] = trans.merchant
                # Yup weird. '0' means new?
                itemized_split['txnId{}'.format(i)] = 0
                if not ignore_category:
                    itemized_split['category{}'.format(i)] = trans.category
                    itemized_split['categoryId{}'.format(i)] = (
                        trans.category_id)

            logger.debug('Sending a "split" transaction request: {}'.format(
                itemized_split))
            response = mint_client.post(
                '{}{}'.format(
                    MINT_ROOT_URL,
                    UPDATE_TRANS_ENDPOINT),
                data=itemized_split)
            json_resp = response.json()
            # The first id is always the original transaction (now
            # parent transaction id).
            new_trans_ids = json_resp['txnId'][1:]
            assert len(new_trans_ids) == len(new_trans)
            for itemized_id, trans in zip(new_trans_ids, new_trans):
                # Now send the note for each itemized transaction.
                itemized_note = {
                    'task': 'txnedit',
                    'txnId': '{}:0'.format(itemized_id),
                    'note': trans.note,
                    'token': mint_client.token,
                }
                note_response = mint_client.post(
                    '{}{}'.format(
                        MINT_ROOT_URL,
                        UPDATE_TRANS_ENDPOINT),
                    data=itemized_note)
                logger.debug(
                    'Received note response: {}'.format(note_response.text))

            updateProgress.next()
            logger.debug('Received response: {}'.format(response.text))
            num_requests += 1

    updateProgress.finish()

    dur = s_to_time(time.time() - start_time)
    logger.info('Sent {} updates to Mint in {}'.format(num_requests, dur))


def s_to_time(s):
    s = int(s)
    dur_s = int(s % 60)
    dur_m = int(s / 60) % 60
    dur_h = int(s // 60 // 60)
    return datetime.time(hour=dur_h, minute=dur_m, second=dur_s)


def define_args(parser):
    # Mint creds:
    parser.add_argument(
        '--mint_email', default=None,
        help=('Mint e-mail address for login. If not provided here, will be '
              'prompted for user.'))
    parser.add_argument(
        '--mint_password', default=None,
        help=('Mint password for login. If not provided here, will be '
              'prompted for.'))

    # Inputs:
    parser.add_argument(
        'items_csv', type=argparse.FileType('r'),
        help='The "Items" Order History Report from Amazon')
    parser.add_argument(
        'orders_csv', type=argparse.FileType('r'),
        help='The "Orders and Shipments" Order History Report from Amazon')
    parser.add_argument(
        '--refunds_csv', type=argparse.FileType('r'),
        help='The "Refunds" Order History Report from Amazon. '
             'This is optional.')

    # To itemize or not to itemize; that is the question:
    parser.add_argument(
        '--verbose_itemize', action='store_true',
        help=('Default behavior is to not itemize out shipping/promos/etc if '
              'there is only one item per Mint transaction. Will also remove '
              'free shipping. Set this to itemize everything.'))
    parser.add_argument(
        '--no_itemize', action='store_true',
        help=('Do not split Mint transactions into individual items with '
              'attempted categorization.'))

    # Debugging/testing.
    parser.add_argument(
        '--pickled_epoch', type=int,
        help=('Do not fetch categories or transactions from Mint. Use this '
              'pickled epoch instead. If coupled with --dry_run, no '
              'connection to Mint is established.'))
    parser.add_argument(
        '--dry_run', action='store_true',
        help=('Do not modify Mint transaction; instead print the proposed '
              'changes to console.'))
    parser.add_argument(
        '--skip_dry_print', action='store_true',
        help=('Do not print dry run results (useful for development).'))
    parser.add_argument(
        '--num_updates', type=int,
        default=0,
        help=('Only send the first N updates to Mint (or print N updates at '
              'dry run). If not present, all updates are sent or printed.'))

    # Retag transactions that have already been tagged previously:
    parser.add_argument(
        '--prompt_retag', action='store_true',
        help=('For transactions that have been previously tagged by this '
              'script, override any edits (like adjusting the category) but '
              'only after confirming each change. More gentle than '
              '--retag_changed'))

    parser.add_argument(
        '--retag_changed', action='store_true',
        help=('For transactions that have been previously tagged by this '
              'script, override any edits (like adjusting the category). This '
              'feature works by looking for "Amazon.com: " at the start of a '
              'transaction. If the user changes the description, then the '
              'tagger won\'t know to leave it alone.'))
    parser.add_argument(
        '--print_unmatched', action='store_true',
        help=('At completion, print unmatched orders to help manual tagging.'))

    # Prefix customization:
    parser.add_argument(
        '--description_prefix_override', type=str,
        help=('The prefix to use when updating the description for each Mint '
              'transaction. By default, the \'Website\' value from Amazon '
              'Items/Orders csv is used. If a string is provided, use '
              'this instead for all matched transactions. If given, this is '
              'used in conjunction with amazon_domains to detect if a '
              'transaction has already been tagged by this tool.'))
    parser.add_argument(
        '--description_return_prefix_override', type=str,
        help=('The prefix to use when updating the description for each Mint '
              'refund. By default, the \'Website\' value from Amazon '
              'Items/Orders csv is used with refund appended (e.g. '
              '\'Amazon.com Refund: ...\'. If a string is provided here, use '
              'this instead for all matched refunds. If given, this is '
              'used in conjunction with amazon_domains to detect if a '
              'refund has already been tagged by this tool.'))
    parser.add_argument(
        '--amazon_domains', type=str,
        # From: https://en.wikipedia.org/wiki/Amazon_(company)#Website
        default=('amazon.com,amazon.cn,amazon.in,amazon.co.jp,amazon.com.sg,'
                 'amazon.com.tr,amazon.fr,amazon.de,amazon.it,amazon.nl,'
                 'amazon.es,amazon.co.uk,amazon.ca,amazon.com.mx,'
                 'amazon.com.au,amazon.com.br'),
        help=('A list of all valid Amazon domains/websites. These should '
              'match the website column from Items/Orders and is used to '
              'detect if a transaction has already been tagged by this tool.'))

    parser.add_argument(
        '--mint_input_merchant_filter', type=str,
        default='amazon,amzn',
        help=('Only consider Mint transactions that have one of these strings '
              'in the merchant field. Case-insensitive comma-separated.'))
    parser.add_argument(
        '--mint_input_categories_filter', type=str,
        help=('If present, only consider Mint transactions that match one of '
              'the given categories here. Comma separated list of Mint '
              'categories.'))

    # Tagging options:
    parser.add_argument(
        '--no_tag_categories', action='store_true',
        help=('If present, do not update Mint categories. This is useful as '
              'Amazon doesn\'t provide the best categorization and it is '
              'pretty common user behavior to manually change the categories. '
              'This flag prevents tagger from wiping out that user work.'))
    parser.add_argument(
        '--do_not_predict_categories', action='store_true',
        help=('Do not attempt to predict custom category tagging based on any '
              'tagging overrides. By default (no arg) tagger will attempt to '
              'find items that you have manually changed categories for.'))


if __name__ == '__main__':
    main()
