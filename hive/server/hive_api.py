"""[WIP] New and improved discussions API supporting user context."""
import time
import logging

from decimal import Decimal
from aiocache import cached
from hive.db.adapter import Db

log = logging.getLogger(__name__)

DB = Db.instance()

async def db_head_state():
    """Status/health check."""
    sql = ("SELECT num, created_at, extract(epoch from created_at) ts "
           "FROM hive_blocks ORDER BY num DESC LIMIT 1")
    row = DB.query_row(sql)
    return dict(db_head_block=row['num'],
                db_head_time=str(row['created_at']),
                db_head_age=int(time.time() - row['ts']))


# stats methods
# -------------

@cached(ttl=7200)
async def payouts_total():
    """Get total sum of all completed payouts."""
    # memoized historical sum. To update:
    #  SELECT SUM(payout) FROM hive_posts_cache
    #  WHERE is_paidout = 1 AND payout_at <= precalc_date
    precalc_date = '2017-08-30 00:00:00'
    precalc_sum = Decimal('19358777.541')

    # sum all payouts since `precalc_date`
    sql = """
      SELECT SUM(payout) FROM hive_posts_cache
      WHERE is_paidout = '1' AND payout_at > '%s'
    """ % (precalc_date)

    return float(precalc_sum + DB.query_one(sql)) #TODO: decimal

@cached(ttl=3600)
async def payouts_last_24h():
    """Sum of completed payouts in the last 24 hours."""
    sql = """
      SELECT SUM(payout) FROM hive_posts_cache WHERE is_paidout = '1'
      AND payout_at > (NOW() AT TIME ZONE 'utc') - INTERVAL '24 HOUR'
    """
    return float(DB.query_one(sql)) # TODO: decimal


# discussion apis
# ---------------

async def get_blog_feed(account: str, skip: int, limit: int, ctx: str = None):
    """Get a blog feed (posts and reblogs from the specified account)"""
    account_id = _get_account_id(account)
    sql = ("SELECT post_id FROM hive_feed_cache WHERE account_id = :account_id "
           "ORDER BY created_at DESC LIMIT :limit OFFSET :skip")
    post_ids = DB.query_col(sql, account_id=account_id, skip=skip, limit=limit)
    return _get_posts(post_ids, ctx)


async def get_related_posts(account: str, permlink: str):
    """Get related trending posts.

    Based on the provided post's primary tag."""

    sql = """
      SELECT p2.id
        FROM hive_posts p1
        JOIN hive_posts p2 ON p1.category = p2.category
        JOIN hive_posts_cache pc ON p2.id = pc.post_id
       WHERE p1.author = :a AND p1.permlink = :p
         AND sc_trend > :t AND p1.id != p2.id
    ORDER BY sc_trend DESC LIMIT 5
    """
    thresh = time.time() / 480000
    post_ids = DB.query_col(sql, a=account, p=permlink, t=thresh)
    return _get_posts(post_ids)


# ---

def _get_account_id(name):
    return DB.query_one("SELECT id FROM hive_accounts WHERE name = :n", n=name)

# given an array of post ids, returns full metadata in the same order
def _get_posts(ids, ctx=None):
    sql = """
    SELECT post_id, author, permlink, title, preview, img_url, payout,
           promoted, created_at, payout_at, is_nsfw, rshares, votes, json
      FROM hive_posts_cache WHERE post_id IN :ids
    """

    reblogged_ids = []
    if ctx:
        reblogged_ids = DB.query_col("SELECT post_id FROM hive_reblogs "
                                     "WHERE account = :a AND post_id IN :ids",
                                     a=ctx, ids=tuple(ids))

    # key by id so we can return sorted by input order
    posts_by_id = {}
    for row in DB.query_all(sql, ids=tuple(ids)):
        obj = dict(row)

        if ctx:
            voters = [csa.split(",")[0] for csa in obj['votes'].split("\n")]
            obj['user_state'] = {
                'reblogged': row['post_id'] in reblogged_ids,
                'voted': ctx in voters
            }

        # TODO: Object of type 'Decimal' is not JSON serializable
        obj['payout'] = float(obj['payout'])
        obj['promoted'] = float(obj['promoted'])

        # TODO: Object of type 'datetime' is not JSON serializable
        obj['created_at'] = str(obj['created_at'])
        obj['payout_at'] = str(obj['payout_at'])

        obj.pop('votes') # temp
        obj.pop('json')  # temp
        posts_by_id[row['post_id']] = obj

    # in rare cases of cache inconsistency, recover and warn
    missed = set(ids) - posts_by_id.keys()
    if missed:
        log.warning("_get_posts do not exist in cache: %s", repr(missed))
        for _id in missed:
            ids.remove(_id)

    return [posts_by_id[_id] for _id in ids]
