import concurrent
import datetime
import json

from sqlalchemy.exc import SQLAlchemyError

from api import logging
from api import cache
from api import executor
from api import db

import time
import requests

DATE_FORMAT_STRING = "%Y-%m-%d %H:%M:%S"
DATE_FROM_STRING_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DATE_FROM_STRING_SPLIT_FORMAT = "%Y-%m-%dT%H:%M:%S"


def create_session():
    return db.session


def _format_video(video):
    if 'http' in video:
        return video
    return 'https://ipfs.hivebp.io/ipfs/{}'.format(video.replace('video:', '').strip())


def _format_image(image):
    if image and 'http' not in image and 'video:' not in image:
        return 'https://ipfs.hivebp.io/ipfs/{}'.format(image)
    return image if image else ''


def _format_author_thumbnail(author, original=None, size=80):
    return _format_collection_thumbnail(original, author, size) if original else original


def _format_thumbnail(image):
    if not image:
        return image
    if 'video:' in image:
        return 'https://ipfs.hivebp.io/video?hash={}'.format(
            image.replace('DUNGEONS-&-DRAGONS', 'DUNGEONS-%26-DRAGONS').replace('video:', '').strip())
    else:
        return 'https://ipfs.hivebp.io/thumbnail?hash={}'.format(
            image.replace('DUNGEONS-&-DRAGONS', 'DUNGEONS-%26-DRAGONS'))


def _format_collection_thumbnail(image, collection, size):
    if not image:
        return image
    return 'https://ipfs.hivebp.io/preview?collection={}&size={}&hash={}'.format(
        collection, size, image.replace('video:', '').replace('DUNGEONS-&-DRAGONS', 'DUNGEONS-%26-DRAGONS').strip()
    )


def _format_preview(image):
    return 'https://ipfs.hivebp.io/preview/{}'.format(image)


def _format_banner(image):
    return 'https://ipfs.hivebp.io/nfthive?ipfs={}'.format(image)


def _parse_mdata(asset):
    return json.loads(asset.mdata.replace('""', '","').replace('”', '"')) if asset.mdata else None


def _get_name(asset):
    mdata = _parse_mdata(asset)
    return mdata['name'] if mdata and 'name' in mdata.keys() else None


def _get_image(asset):
    mdata = _parse_mdata(asset)
    return mdata['img'] if mdata and 'img' in mdata.keys() else None


def to_camel_case(snake_str):
    return "".join(x.capitalize() for x in snake_str.lower().split("_"))


def to_lower_camel_case(snake_str):
    camel_string = to_camel_case(snake_str)
    return snake_str[0].lower() + camel_string[1:]


def _get_assets_object():
    return (
        'array_agg(json_build_object(\'asset_id\', a.asset_id, \'name\', n.name, \'collection\', a.collection, '
        '\'schema\', a.schema, \'mutable_data\', m.data, \'immutable_data\', i.data, \'template_immutable_data\', '
        'td.data, \'num_burned\', ts.num_burned, \'avg_wax_price\', ts.avg_wax_price, \'avg_usd_price\', '
        'ts.avg_usd_price, \'last_sold_wax\', ts.last_sold_wax, \'last_sold_usd\', ts.last_sold_usd, '
        '\'last_sold_listing_id\', last_sold_listing_id, '
        '\'last_sold_timestamp\', ts.last_sold_timestamp, \'floor_price\', fp.floor_price, '
        '\'volume_wax\', ts.volume_wax, \'volume_usd\', ts.volume_usd, \'num_sales\', ts.num_sales, '
        '\'num_minted\', ts.total, \'favorited\', f.user_name IS NOT NULL, '
        '\'template_id\', t.template_id, \'image\', img.image, \'video\', vid.video, '
        '\'universal_preview_image\', CASE WHEN up1.image_id IS NULL THEN img.image ELSE NULL END, '
        '\'universal_preview_video\', CASE WHEN up2.video_id IS NULL THEN vid.video ELSE NULL END, '
        '\'mint\', a.mint, \'mint_timestamp\', a.timestamp, \'mint_block_num\', a.block_num, \'mint_seq\', a.seq, '
        '\'rarity_score\', p.rarity_score, \'num_traits\', p.num_traits, \'rank\', p.rank, '
        '\'traits\', {attributes_obj})) AS assets '.format(attributes_obj=_get_attributes_object())
    )


def _get_badges_object(prefix='a.'):
    return (
        '(SELECT array_agg(json_build_object(\'name\', b.name, \'level\', b.level, \'value\', b.value, '
        '\'timestamp\', b.timestamp)) FROM badges b WHERE collection = {}collection) AS badges '.format(prefix)
    )


def _get_tags_object(prefix='a.'):
    return (
        '(SELECT array_agg(json_build_object(\'tag_id\', tg.tag_id, \'tag_name\', tg.tag_name)) '
        'FROM tags_mv tg WHERE collection = {}collection) AS tags '.format(prefix)
    )


def _get_attributes_object():
    return (
        '(SELECT array_agg(json_build_object(\'attribute_id\', attribute_id, '
        '\'attribute_name\', attribute_name, \'string_value\', string_value, \'int_value\', int_value, '
        '\'float_value\', float_value, \'bool_value\', bool_value, \'floor_price\', floor_wax, '
        '\'rarity_score\', rarity_score, \'total_schema\', total_schema)) '
        'FROM assets '
        'INNER JOIN attributes ON attribute_id = ANY(attribute_ids) '
        'LEFT JOIN attribute_stats USING(attribute_id) '
        'WHERE asset_id = a.asset_id) '
    )


def isfloat(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def construct_category_clause(
    session, format_dict, collection, schema, attributes, prefix
):
    category_clause = ''

    attribute_ids = []
    if attributes:
        attr_arr = attributes.split(',')
        for attr in attr_arr:
            key = attr.split(':')[0]
            value = attr.split(':')[1]
            attr_sql = 'SELECT attribute_id FROM attributes WHERE collection = :collection '

            attr_dict = {
                'collection': collection
            }

            if schema:
                if ',' in schema:
                    attr_dict['schemas'] = tuple(schema.split(','))
                    attr_sql += ' AND schema IN :schemas'
                else:
                    attr_dict['schema'] = schema
                    attr_sql += ' AND schema = :schema'

            attr_dict['attribute_name'] = key

            if isinstance(value, bool) or value in ['f', 't']:
                column = 'bool_value'
            elif isinstance(value, int) or value.isnumeric():
                column = 'int_value'
                value = int(value)
                if value > 9223372036854775807 or value < -9223372036854775808:
                    value = str(value)
                    column = 'string_value'
            elif isinstance(value, float) or isfloat(value):
                column = 'float_value'
                value = float(value)
            else:
                column = 'string_value'

            attr_sql += ' AND attribute_name = :attribute_name AND {column} = :value '.format(column=column)

            attr_dict['value'] = value

            res = session.execute(attr_sql, attr_dict)

            for attribute_id in res:
                attribute_ids.append(attribute_id['attribute_id'])

    if len(attribute_ids) > 1:
        category_clause += ' AND :attribute_ids <@ {}attribute_ids '.format(prefix)
        format_dict['attribute_ids'] = attribute_ids
    elif len(attribute_ids) == 1:
        category_clause += ' AND :attribute_id = ANY({}attribute_ids) '.format(prefix)
        format_dict['attribute_id'] = attribute_ids[0]
    elif attributes and len(attribute_ids) == 0:
        category_clause += ' AND FALSE '

    return category_clause


def _format_date(date):
    return datetime.datetime.strptime(date, '%Y-%m-%dT%H:%M:%S' + ('.%f' if '.' in date else '')).strftime(
        DATE_FORMAT_STRING) if isinstance(date, str) else date.strftime(DATE_FORMAT_STRING)


def _format_tags(tags):
    tags_arr = []
    for tag in tags:
        if tag['tag_id']:
            tags_arr.append({
                'tagId': tag['tag_id'],
                'name': tag['tag_name']
            })
    return tags_arr


def _format_badges(badges):
    badge_arr = []
    for badge in badges:
        if badge['name']:
            badge_arr.append({
                'name': badge['name'],
                'level': badge['level'],
                'value': badge['value'],
                'timestamp': datetime.datetime.strptime(
                    badge['timestamp'].split('.')[0], DATE_FROM_STRING_SPLIT_FORMAT
                ).strftime(DATE_FORMAT_STRING) if badge['timestamp'] else None
            })
    return badge_arr


def _format_traits(traits):
    traits_arr = []
    for trait in traits:
        if trait['attribute_name']:
            trait_obj = {
                'name': trait['attribute_name']
            }
            if trait['rarity_score']:
                trait_obj['rarityScore'] = trait['rarity_score']
            if trait['total_schema']:
                trait_obj['totalSchema'] = trait['total_schema']
            if trait['floor_price']:
                trait_obj['floorPrice'] = trait['floor_price']
            if trait['string_value']:
                trait_obj['value'] = trait['string_value']
            elif trait['int_value'] or trait['int_value'] == 0:
                trait_obj['value'] = trait['int_value']
            elif trait['float_value'] or trait['float_value'] == 0.0:
                trait_obj['value'] = trait['float_value']
            else:
                trait_obj['value'] = trait['bool_value']
            traits_arr.append(trait_obj)
    return traits_arr


def _format_asset(asset):
    try:
        asset_obj = {
            'assetId': asset['asset_id'],
            'name': asset['name'],
            'schema': asset['schema'],
            'mint': asset['mint'],
            'mutableData': json.loads(asset['mutable_data']) if asset['mutable_data'] else '{}',
            'immutableData': json.loads(asset['immutable_data']) if asset['immutable_data'] else '{}',
            'createdAt': {
                'date': _format_date(asset['mint_timestamp']),
                'block': asset['mint_block_num'],
                'globalSequence': asset['mint_seq'],
            },
            'traits': _format_traits(asset['traits']) if asset['traits'] else [],
        }
        if 'collection_name' in asset.keys():
            asset_obj['collection'] = {
                'collection': asset['collection'],
                'collectionName': asset['collection_name'],
                'collectionImage': asset['collection_image'],
                'tags': _format_tags(asset['tags']) if asset['tags'] else [],
                'badges': _format_badges(asset['badges']) if asset['badges'] else []
            }
        if asset['rarity_score']:
            asset_obj['rarityScore'] = asset['rarity_score']
            asset_obj['rank'] = asset['rank']
            asset_obj['numTraits'] = asset['num_traits']
        if asset['universal_preview_video']:
            asset_obj['previewImage'] = _format_collection_thumbnail(
                asset['universal_preview_video'], asset['collection'], 240)
        elif asset['universal_preview_image']:
            asset_obj['previewImage'] = _format_collection_thumbnail(
                asset['universal_preview_image'], asset['collection'], 240)
        if 'template_id' in asset.keys() and asset['template_id']:
            stats_obj = {}
            if asset['avg_wax_price'] and asset['avg_usd_price']:
                stats_obj['averageWaxPrice'] = asset['avg_wax_price']
                stats_obj['averageUsd'] = asset['avg_usd_price']
            if asset['last_sold_wax'] and asset['last_sold_usd']:
                stats_obj['lastSoldWax'] = asset['last_sold_wax']
                stats_obj['lastSoldUsd'] = asset['last_sold_usd']
            if asset['last_sold_timestamp'] and asset['last_sold_listing_id'] and asset['last_sold_wax'] and asset[
                    'last_sold_usd']:
                stats_obj['lastSold'] = {
                    'date': _format_date(asset['last_sold_timestamp']),
                    'listingId': asset['last_sold_listing_id'],
                    'priceWax': asset['last_sold_wax'],
                    'priceUsd': asset['last_sold_usd']
                }
            if asset['volume_wax'] and asset['volume_usd']:
                stats_obj['volumeWax'] = asset['volume_wax']
                stats_obj['volumeUsd'] = asset['volume_usd']
            if asset['num_sales']:
                stats_obj['numSales'] = asset['num_sales']
            if asset['num_burned']:
                stats_obj['numBurned'] = asset['num_burned']
            if asset['num_minted']:
                stats_obj['numMinted'] = asset['num_minted']
            if asset['floor_price']:
                stats_obj['floorPrice'] = asset['floor_price']
            asset_obj['template'] = {
                'templateId': asset['template_id'],
                'immutableData': asset['template_immutable_data'],
                'stats': stats_obj,
            }
        return asset_obj
    except Exception as e:
        print(e)


def _format_schema(schema):
    asset_obj = {
        'schema': schema['schema'],
        'stats': {
            'numTemplates': schema['num_templates'],
            'numAssets': schema['num_assets'],
            'numBurned': schema['num_burned'],
            'volumeWAX': schema['volume_wax'],
            'volumeUSD': schema['volume_usd'],
        },
        'collection': {
            'collection': schema['collection'],
            'collectionName': schema['collection_name'],
            'collectionImage': schema['collection_image'],
            'tags': _format_tags(schema['tags']) if schema['tags'] else [],
            'badges': _format_badges(schema['badges']) if schema['badges'] else []
        },
        'createdAt': {
            'date': schema['created_timestamp'].strftime(DATE_FORMAT_STRING),
            'block': schema['created_block_num'],
            'globalSequence': schema['created_seq'],
        }
    }
    return asset_obj


def _format_assets_object(items):
    assets = []

    for asset in items:
        assets.append(_format_asset(asset))

    return assets


def _format_listings(item):
    return {
        'date': item['timestamp'].strftime(DATE_FORMAT_STRING),
        'timestamp': datetime.datetime.timestamp(item['timestamp']), 'price': item['price'],
        'usdWax': item['usd_wax'], 'currency': item['currency'], 'market': item['market'],
        'maker': item['maker'], 'seller': item['seller'], 'listingId': item['listing_id'],
        'uniqueSaleId': item['sale_id'],
        'collection': {
            'collection': item['collection'],
            'collectionName': item['collection_name'],
            'collectionImage': item['collection_image'],
            'verification': item['verified']
        },
        'assets': _format_assets_object(item['assets'])
    }


def _get_search_term(session, term):
    template_id = None
    asset_id = None
    name = None
    collection = None
    if (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) < 1099511627776:
        template = session.execute(
            'SELECT template_id, collection FROM templates WHERE template_id = :term', {
                'term': term
            }
        ).first()
        if template:
            template_id = template['template_id']
            collection = template['collection']
    elif (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) >= 1099511627776:
        asset = session.execute(
            'SELECT asset_id, collection FROM assets WHERE asset_id = :term', {
                'term': term
            }
        ).first()
        if asset:
            asset_id = asset['asset_id']
            collection = asset['collection']
    else:
        name = term

    return name, asset_id, template_id, collection


def _add_mint_filter(min_mint, max_mint, search_clause, format_dict):
    if min_mint and max_mint:
        search_clause += (
            'AND a.mint BETWEEN :min_mint AND :max_mint '
        )
        format_dict['min_mint'] = min_mint
        format_dict['max_mint'] = max_mint
    elif min_mint:
        search_clause += (
            'AND a.mint >= :min_mint '
        )
        format_dict['min_mint'] = min_mint
    elif max_mint:
        search_clause += (
            'AND a.mint >= :max_mint '
        )
        format_dict['max_mint'] = max_mint
    return search_clause


def _add_recently_sold_filter(recently_sold, search_clause):
    if recently_sold:
        table = 'recently_sold_month_mv'
        if recently_sold == 'hour':
            table = 'recently_sold_hour_mv'
        elif recently_sold == 'day':
            table = 'recently_sold_day_mv'
        elif recently_sold == 'week':
            table = 'recently_sold_week_mv'

        search_clause += (
            ' AND a.template_id IN ('
            '   SELECT template_id FROM {table} WHERE template_id = a.template_id'
            ') '.format(table=table)
        )
    return search_clause


def parallel(requests):
    """Execute requests in parallel.

    Provided a dict of requests, execute them with the provided id and kwargs.
    Return the results of these requests in a dict keyed by keys in the
    requests parameter.
    """
    futures = {
        executor.submit(
            request['func'],
            *request['args']
        ): key for (key, request) in requests.items()
    }
    concurrent.futures.wait(futures)
    output = dict(map(lambda f: (futures[f], f.result()), futures))

    # Format the responses for errors, return as oto response
    return output


def newest_listings(collection, schema, template_id):
    global last_reported_order

    try:
        sales_results = listings(
            term=template_id, category=schema, collection=collection,
            order_by='date_desc', limit=24, search_type='sales'
        )

        sales = []

        max_order = 0

        has_new_element = False
        for sale in sales_results:
            new_sale = {}

            for key in sale.keys():
                if key != 'displayData':
                    if sale[key]:
                        new_sale[key] = sale[key].replace('\'', '') if isinstance(sale[key], str) else sale[key]

            if new_sale['verified']:
                new_sale['verified'] = 1
            new_sale.pop('mdata', None)

            sales.append(new_sale)

            if new_sale['orderId'] > max_order:
                max_order = new_sale['orderId']

            if template_id not in last_reported_order.keys():
                last_reported_order[template_id] = 0

            if max_order > last_reported_order[template_id]:
                last_reported_order[template_id] = max_order
                has_new_element = True

        return {'hasNewElement': 1 if has_new_element else 0, 'elements': sales}
    except Exception as e:
        print(e)
        return {'hasNewElement': 0, 'elements': []}


def _parse_order(order_by):
    order_dir = 'ASC'

    if '_asc' in order_by:
        order_dir = 'ASC'
        order_by = order_by.replace('_asc', '')
    elif '_desc' in order_by:
        order_dir = 'DESC'
        order_by = order_by.replace('_desc', '')

    return order_dir, order_by


def schemas(
    term=None, collection=None, schema=None, limit=100, order_by='name_asc', exact_search=False, offset=0,
    verified='verified'
):
    session = create_session()

    order_dir, order_by = _parse_order(order_by)

    try:
        format_dict = {'limit': limit, 'offset': offset}

        limit_clause = 'LIMIT :limit OFFSET :offset'

        search_clause = ''
        order_clause = ''
        personal_blacklist_clause = ''
        search_category_clause = ''

        if collection:
            format_dict['collection'] = collection

            search_category_clause += ' AND a.collection = :collection '

            if schema:
                format_dict['schema'] = schema
                search_category_clause += ' AND a.schema = :schema '
            search_clause += search_category_clause

        if term:
            if exact_search:
                search_clause += (
                    ' AND n.name = :search_name '
                )
                format_dict['search_name'] = '{}'.format(term)
            else:
                search_clause += (
                    ' AND n.name LIKE :search_name '
                )
                format_dict['search_name'] = '%{}%'.format(term)

        source_clause = (
            'schemas a '
        )

        columns_clause = (
            'a.schema, cn.name AS collection_name, a.collection, schema_format, {badges_object}, {tags_obj}, '
            'ci.image as collection_image, a.timestamp AS created_timestamp, a.block_num AS created_block_num, '
            'a.seq AS created_seq, ts.num_assets, ts.num_templates, ts.num_burned, ts.volume_wax, '
            'ts.volume_usd, (SELECT MIN(floor_price) '
            '   FROM templates t '
            '   INNER JOIN floor_prices_mv USING(template_id) '
            '   WHERE t.collection = a.collection AND t.schema = a.schema'
            ') AS floor_price'.format(
                badges_object=_get_badges_object(), tags_obj=_get_tags_object()
            )
        )

        if verified == 'verified':
            search_clause += ' AND col.verified '
        elif verified == 'unverified':
            search_clause += (
                ' AND ('
                '   (col.verified IS NULL AND col.blacklisted IS NULL) OR (NOT col.verified AND NOT col.blacklisted)'
                ') '
            )
        elif verified == 'all':
            search_clause += ' AND (NOT col.blacklisted OR col.blacklisted IS NULL) '
        elif verified == 'blacklisted':
            search_clause += ' AND col.blacklisted '

        if order_by == 'date':
            order_clause = 'ORDER BY a.seq {}'.format(order_dir)
        elif order_by == 'collection':
            order_clause = (
                'ORDER BY a.collection {}'
            ).format(order_dir)
        elif order_by == 'num_templates':
            order_clause = 'ORDER BY num_templates ' + order_dir
        elif order_by == 'num_assets':
            order_clause = 'ORDER BY num_assets ' + order_dir
        elif order_by == 'volume':
            order_clause = 'ORDER BY volume_wax ' + order_dir

        sql = (
            '   SELECT {columns_clause} '
            '   FROM {source_clause} '
            '   LEFT JOIN collections col USING (collection) '
            '   LEFT JOIN schema_stats_mv ts USING (collection, schema) '
            '   LEFT JOIN names cn ON (col.name_id = cn.name_id) '
            '   LEFT JOIN images ci ON (col.image_id = ci.image_id) '
            '   WHERE TRUE {search_clause} '
            '   {personal_blacklist_clause} '
            '   {order_clause} {limit_clause}'.format(
                columns_clause=columns_clause,
                source_clause=source_clause,
                search_clause=search_clause,
                order_clause=order_clause,
                limit_clause=limit_clause,
                personal_blacklist_clause=personal_blacklist_clause
            ))

        res = session.execute(sql, format_dict)

        results = []

        for row in res:
            try:
                result = _format_schema(row)

                results.append(result)
            except Exception as e:
                logging.error(e)

        return results
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def get_health():
    session = create_session()
    try:
        result = session.execute(
            'SELECT block_num, timestamp '
            'FROM chronicle_transactions '
            'WHERE seq = (SELECT MAX(seq) FROM chronicle_transactions)'
        ).first()

        if result:
            return {
                'success': 'true',
                'block_num': result['block_num'],
                'timestamp': result['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            }

        return {
            'success': 'false'
        }
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def assets(
    term=None, owner=None, collection=None, schema=None, tags=None, limit=100, order_by='date_desc',
    exact_search=False, search_type='assets', min_average=None, max_average=None, min_mint=None, max_mint=None,
    contract=None, offset=0, verified='verified', user='', favorites=False, backed=False, recently_sold=None,
    attributes=None
):
    session = create_session()

    name, asset_id, template_id, col = _get_search_term(session, term)

    if not collection:
        collection = col

    order_dir, order_by = _parse_order(order_by)

    if (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) < 10000000000000:
        template = session.execute('SELECT template_id FROM templates WHERE template_id = :term', {
            'term': term
        }).first()
        if template:
            template_id = template['template_id']
    elif (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) >= 10000000000000:
        asset = session.execute('SELECT asset_id FROM assets WHERE asset_id = :term', {
            'term': term
        }).first()
        if asset:
            asset_id = asset['asset_id']
    else:
        name = term

    try:
        format_dict = {'user': user, 'limit': limit, 'offset': offset}

        limit_clause = 'LIMIT :limit OFFSET :offset'

        join_clause = (
            'LEFT JOIN favorites f ON ((f.asset_id = a.asset_id OR f.template_id = a.template_id) '
            'AND f.user_name = :user) '
        ) if user else (
            'LEFT JOIN favorites f ON (f.asset_id IS NULL AND f.user_name IS NULL) '
        )

        search_clause = ''
        order_clause = ''
        with_clause = ''
        personal_blacklist_clause = ''
        search_category_clause = ''

        if collection:
            format_dict['collection'] = collection

            search_category_clause += ' AND a.collection = :collection '

            if schema:
                format_dict['schema'] = schema
                search_category_clause += ' AND a.schema = :schema '
                search_category_clause += construct_category_clause(
                    session, format_dict, collection, schema, attributes, 'a.'
                )
            search_clause += search_category_clause

        if name:
            if exact_search:
                search_clause += (
                    ' AND n.name = :search_name '
                )
                format_dict['search_name'] = '{}'.format(name)
            else:
                search_clause += (
                    ' AND n.name LIKE :search_name '
                )
                format_dict['search_name'] = '%{}%'.format(name)

        if asset_id:
            format_dict['asset_id'] = '{}'.format(asset_id)
            search_clause += ' AND a.asset_id = :asset_id '

        if template_id:
            format_dict['template_id'] = template_id
            search_clause += (
                ' AND a.template_id = :template_id '
            )

        source_clause = (
            'assets a '
        )
        columns_clause = (
            'a.asset_id, a.template_id, n.name, a.schema, f.user_name IS NOT NULL AS favorited, '
            'm.data AS mutable_data, i.data AS immutable_data, td.data AS template_immutable_data, ts.num_burned, '
            'a.mint, ts.avg_wax_price, ts.avg_usd_price, ts.last_sold_wax, ts.last_sold_usd, last_sold_listing_id, '
            'ts.last_sold_timestamp AS last_sold_timestamp, fp.floor_price, ts.volume_wax, '
            'ts.volume_usd, ts.num_sales, ts.total AS num_minted, t.template_id, img.image, vid.video,'
            'CASE WHEN up1.image_id IS NULL THEN img.image ELSE NULL END AS universal_preview_image, '
            'CASE WHEN up2.video_id IS NULL THEN vid.video ELSE NULL END AS universal_preview_video, '
            'cn.name AS collection_name, a.collection, ci.image as collection_image, a.timestamp AS mint_timestamp, '
            'a.block_num AS mint_block_num, a.seq AS mint_seq, p.rarity_score, p.num_traits, p.rank, '
            '{badges_object}, {tags_obj}, {attributes_obj} AS traits'.format(
                badges_object=_get_badges_object(), tags_obj=_get_tags_object(), attributes_obj=_get_attributes_object()
            )
        )
        if search_type == 'packs':
            search_clause += (
                ' AND EXISTS (SELECT template_id FROM packs p WHERE template_id = a.template_id) '
            )
        if search_type == 'pfps':
            search_clause += (
                'AND p.asset_id IS NOT NULL '
            )

        if verified == 'verified':
            search_clause += ' AND col.verified '
        elif verified == 'unverified':
            search_clause += (
                ' AND ((ac.verified IS NULL AND ac.blacklisted IS NULL) OR (NOT ac.verified AND NOT ac.blacklisted))'
            )
        elif verified == 'all':
            search_clause += ' AND (NOT ac.blacklisted OR ac.blacklisted IS NULL) '
        elif verified == 'blacklisted':
            search_clause += ' AND ac.blacklisted '

        if tags:
            tag_ids = tags.split(',')
            if len(tag_ids) == 1:
                search_clause += (
                    'AND EXISTS ('
                    '    SELECT tag_id FROM tags_mv '
                    '    WHERE collection = a.collection '
                    '    AND tag_id = :tag_id'
                    ') '
                )
                format_dict['tag_id'] = tag_ids[0]
            else:
                tag_int_arr = []
                for tag_id in tag_ids:
                    tag_int_arr.append(int(tag_id))
                with_clause += (
                    ', matched_collections AS ('
                    '    SELECT collection FROM collection_tag_ids_mv '
                    '    WHERE :tag_ids <@ tag_ids'
                    ')'
                )
                source_clause += (
                    'INNER JOIN matched_collections USING(collection) '
                )
                format_dict['tag_ids'] = tag_int_arr

        search_clause += _add_mint_filter(min_mint, max_mint, search_clause, format_dict)
        search_clause += _add_recently_sold_filter(recently_sold, search_clause)

        if contract:
            format_dict['contract'] = contract
            search_clause += ' AND a.contract = :contract'
        if favorites:
            search_clause += ' AND f.user_name IS NOT NULL'
        if backed:
            search_clause += ' AND ba.amount IS NOT NULL '
        if owner:
            format_dict['owner'] = '{}'.format(owner.lower().strip())
            if search_type == 'duplicates' or search_type == 'highest_duplicates':
                source_clause = (
                    'my_assets a '
                )
                with_clause += (
                    ', my_assets AS (SELECT a.*, COUNT(*) OVER ('
                    'PARTITION BY a.collection, a.template_id) cnt, MIN(a.asset_id) OVER ('
                    'PARTITION BY a.collection, a.template_id) AS min_mint, MAX(a.asset_id) OVER ('
                    'PARTITION BY a.collection, a.template_id) AS max_mint '
                    'FROM assets a '
                    '{personal_blacklist_clause} '
                    'WHERE owner = :owner AND NOT burned '
                    '{search_clause}) '.format(
                        search_clause=search_clause,
                        personal_blacklist_clause=personal_blacklist_clause
                    )
                )
                if search_type == 'duplicates':
                    search_clause += ' AND cnt > 1 AND a.asset_id > min_mint'
                else:
                    search_clause += ' AND cnt > 1 AND a.asset_id = max_mint'
            elif search_type == 'lowest_mints' or search_type == 'highest_mints':
                source_clause = ' my_assets a '
                with_clause += (
                    ', my_assets AS (SELECT a.*, '
                    'MIN(a.asset_id) OVER (PARTITION BY a.template_id) AS min_mint, '
                    'MAX(a.asset_id) OVER (PARTITION BY a.template_id) AS max_mint '
                    'FROM assets a '
                    '{personal_blacklist_clause} '
                    'WHERE owner = :owner AND a.mint > 0 '
                    '{search_clause}) '.format(
                        search_clause=search_clause,
                        personal_blacklist_clause=personal_blacklist_clause
                    )
                )
                if search_type == 'lowest_mints':
                    search_clause += ' AND a.asset_id = min_mint'
                else:
                    search_clause += ' AND a.asset_id = max_mint'
            else:
                search_clause += ' AND a.owner = :owner '
        if order_by == 'rarity_score' and search_type == 'pfps':
            order_clause = 'ORDER BY p.collection, p.schema, p.rarity_score {}'.format(order_dir)
        elif order_by == 'rarity_score':
            order_clause = 'ORDER BY a.collection, a.schema, p.rarity_score {}'.format(order_dir)
            search_clause += ' AND p.rarity_score IS NOT NULL '
        elif order_by == 'date' and search_type == 'bulk_multi_sell':
            order_clause = 'ORDER BY MAX(a.transferred) {}'.format(order_dir)
        elif order_by == 'owned' and search_type == 'bulk_multi_sell':
            order_clause = 'ORDER BY num_owned {}'.format(order_dir)
        elif order_by == 'date' and search_type == 'staked':
            order_clause = 'ORDER BY s.seq {}'.format(order_dir)
        elif order_by == 'date' and search_type == 'summaries':
            order_clause = 'ORDER BY a.template_id {}'.format(order_dir)
        elif order_by == 'date':
            order_clause = 'ORDER BY a.seq {}'.format(order_dir)
        elif order_by == 'volume' and search_type == 'summaries':
            order_clause = 'ORDER BY tsv.volume_7_days {} NULLS LAST'.format(order_dir)
        elif order_by == 'mint':
            search_clause += ' AND (a.mint IS NOT NULL AND a.mint > 0)'
            if search_type != 'bundles':
                order_clause = 'ORDER BY a1.mint {}'.format(order_dir)
        elif order_by == 'diff':
            search_clause += ' AND lp.price_diff IS NOT NULL '
            if search_type != 'bundles':
                order_clause = 'ORDER BY lp.price_diff {}'.format(order_dir)
        elif order_by == 'template_id':
            search_clause += ' AND a.template_id IS NOT NULL '
            order_clause = (
                'ORDER BY a.template_id {}'
            ).format(order_dir)
        elif order_by == 'collection':
            search_clause += ' AND a.collection IS NOT NULL '
            order_clause = (
                'ORDER BY a.collection {}'
            ).format(order_dir)
        elif order_by == 'average':
            search_clause += ' AND ts.usd_average IS NOT NULL '
            order_clause = (
                'ORDER BY ts.usd_average {}'
            ).format(order_dir)
        elif order_by == 'floor':
            search_clause += ' AND fp.floor_price IS NOT NULL '
            order_clause = (
                'ORDER BY fp.floor_price {}'
            ).format(order_dir)
        elif order_by == 'asset_id':
            order_clause = 'ORDER BY a.asset_id ' + order_dir

        if min_average and max_average:
            search_clause += (
                ' AND ts.usd_average BETWEEN :min_average AND :max_average '
            )
            format_dict['min_average'] = min_average
            format_dict['max_average'] = max_average
        elif min_average:
            search_clause += ' AND ts.usd_average >= :min_average '
            format_dict['min_average'] = min_average
        elif max_average:
            search_clause += ' AND ts.usd_average <= :max_average '
            format_dict['max_average'] = max_average

        sql = (
            'WITH usd_rate AS (SELECT usd FROM usd_prices ORDER BY timestamp DESC LIMIT 1) '
            '{with_clause} '
            'SELECT {columns_clause}, f.user_name IS NOT NULL AS favorited '
            'FROM {source_clause} '
            'LEFT JOIN backed_assets ba USING (asset_id) ' 
            'LEFT JOIN collections col USING (collection) '
            'LEFT JOIN pfp_assets p USING(asset_id) '
            'LEFT JOIN templates t ON (t.template_id = a.template_id) '
            'LEFT JOIN template_stats ts ON (a.template_id = ts.template_id) '
            'LEFT JOIN template_floor_prices_mv fp ON (fp.template_id = a.template_id) '
            'LEFT JOIN names n ON (a.name_id = n.name_id) '
            'LEFT JOIN names tn ON (t.name_id = tn.name_id) '
            'LEFT JOIN names cn ON (col.name_id = cn.name_id) '
            'LEFT JOIN images img ON (a.image_id = img.image_id) '
            'LEFT JOIN images ci ON (col.image_id = ci.image_id) '
            'LEFT JOIN videos vid ON (a.video_id = vid.video_id) '
            'LEFT JOIN universal_previews up1 ON (a.image_id = up1.image_id) '
            'LEFT JOIN universal_previews up2 ON (a.video_id = up2.video_id) '
            'LEFT JOIN data m ON (a.mutable_data_id = m.data_id) '
            'LEFT JOIN data i ON (a.immutable_data_id = i.data_id) '
            'LEFT JOIN data td ON (t.immutable_data_id = td.data_id) '
            '{join_clause} '
            'WHERE TRUE {search_clause} '
            '{personal_blacklist_clause} '
            '{order_clause} {limit_clause}'.format(
                with_clause=with_clause.format(
                    search_clause=search_clause,
                    limit_clause=limit_clause,
                    order_clause=order_clause,
                    columns_clause=columns_clause
                ),
                columns_clause=columns_clause,
                source_clause=source_clause,
                search_clause=search_clause,
                order_clause=order_clause,
                limit_clause=limit_clause,
                join_clause=join_clause,
                personal_blacklist_clause=personal_blacklist_clause
            ))

        res = session.execute(sql, format_dict)

        results = []

        for row in res:
            try:
                results.append(_format_asset(row))
            except Exception as e:
                logging.error(e)

        return results
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def listings(
    term=None, owner=None, market=None, collection=None, schema=None, limit=100, order_by='name_asc',
    exact_search=False, search_type='assets', min_price=None, max_price=None,
    min_mint=None, max_mint=None, contract=None, offset=0,
    verified='verified', user='', favorites=False, backed=False, recently_sold=None,
    attributes=None, pfps_only=False
):
    session = create_session()

    name, asset_id, template_id, col = _get_search_term(session, term)

    if not collection:
        collection = col

    order_dir = 'ASC'

    if '_asc' in order_by:
        order_dir = 'ASC'
        order_by = order_by.replace('_asc', '')
    elif '_desc' in order_by:
        order_dir = 'DESC'
        order_by = order_by.replace('_desc', '')

    if (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) < 1099511627776:
        template = session.execute('SELECT template_id FROM templates WHERE template_id = :term', {
            'term': term
        }).first()
        if template:
            template_id = template['template_id']
    elif (isinstance(term, int) or (isinstance(term, str) and term.isnumeric())) and int(term) >= 1099511627776:
        asset = session.execute('SELECT asset_id FROM assets WHERE asset_id = :term', {
            'term': term
        }).first()
        if asset:
            asset_id = asset['asset_id']
    else:
        name = term

    try:
        format_dict = {'user': user, 'limit': limit, 'offset': offset}

        if not user and search_type in ['missing']:
            return []

        limit_clause = 'LIMIT :limit OFFSET :offset'

        join_clause = (
            'LEFT JOIN favorites f ON ((f.asset_id = a.asset_id OR f.template_id = a.template_id) '
            'AND f.user_name = :user) '
        ) if user else (
            'LEFT JOIN favorites f ON (f.asset_id IS NULL AND f.user_name IS NULL) '
        )

        search_clause = ''
        order_clause = ''
        with_clause = ''
        personal_blacklist_clause = ''
        search_category_clause = ''

        if collection:
            format_dict['collection'] = collection

            search_category_clause += ' AND l.collection = :collection '

            if schema:
                format_dict['schema'] = schema
                search_category_clause += ' AND a.schema = :schema '
                search_category_clause += construct_category_clause(
                    session, format_dict, collection, schema, attributes, 'a.'
                )
            search_clause += search_category_clause

        if name:
            if exact_search:
                search_clause += (
                    ' AND n.name = :search_name '
                )
                format_dict['search_name'] = '{}'.format(name)
            else:
                search_clause += (
                    ' AND n.name LIKE :search_name '
                )
                format_dict['search_name'] = '%{}%'.format(name)

        if asset_id:
            format_dict['asset_id'] = '{}'.format(asset_id)
            search_clause += ' AND :asset_id = ANY(l.asset_ids) '

        if template_id:
            format_dict['template_id'] = template_id
            search_clause += (
                ' AND a.template_id = :template_id '
            )

        with_clause += (
            ', filtered_listings AS ('
            'SELECT l.sale_id, l.market, l.seller, l.timestamp, l.listing_id, l.currency, col.verified, '
            'l.maker, l.collection, l.price, l.collection, array_agg(asset_ids) AS assets '
            'FROM listings l '
            'LEFT JOIN listings_helper_mv h USING (sale_id) '
            'LEFT JOIN assets a ON (a.asset_id = asset_ids[1]) '
            'LEFT JOIN pfp_assets p USING(asset_id) '
            'LEFT JOIN backed_assets ba USING (asset_id) ' 
            'LEFT JOIN collections col ON (col.collection = l.collection) '
            'LEFT JOIN templates t ON (t.template_id = a.template_id) '
            'LEFT JOIN template_stats ts ON (a.template_id = ts.template_id) '
            'LEFT JOIN template_floor_prices_mv fp ON (fp.template_id = a.template_id) '
            'LEFT JOIN names n ON (a.name_id = n.name_id) ' 
            'WHERE TRUE {search_clause} '
            'GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, estimated_wax_price '
            '{group_clause} {order_clause} {limit_clause}) '
        )

        source_clause = (
            'filtered_listings l2 '
            'INNER JOIN listings l USING (sale_id) '
            'LEFT JOIN listings_helper_mv h USING (sale_id) '
            'LEFT JOIN assets a ON (a.asset_id = ANY(asset_ids)) '
            'LEFT JOIN pfp_assets p USING (asset_id) '
            'LEFT JOIN backed_assets ba USING (asset_id) ' 
            'LEFT JOIN collections col ON (col.collection = l.collection) '
            'LEFT JOIN templates t ON (t.template_id = a.template_id) '
            'LEFT JOIN template_stats ts ON (a.template_id = ts.template_id) '
            'LEFT JOIN template_floor_prices_mv fp ON (fp.template_id = a.template_id) '
            'LEFT JOIN names n ON (a.name_id = n.name_id) '
            'LEFT JOIN names tn ON (t.name_id = tn.name_id) '
            'LEFT JOIN names cn ON (col.name_id = cn.name_id) '
            'LEFT JOIN images img ON (a.image_id = img.image_id) '
            'LEFT JOIN images ci ON (col.image_id = ci.image_id) '
            'LEFT JOIN videos vid ON (a.video_id = vid.video_id) '
            'LEFT JOIN universal_previews up1 ON (a.image_id = up1.image_id) '
            'LEFT JOIN universal_previews up2 ON (a.video_id = up2.video_id) '
            'LEFT JOIN data m ON (a.mutable_data_id = m.data_id) '
            'LEFT JOIN data i ON (a.immutable_data_id = i.data_id) '
            'LEFT JOIN data td ON (t.immutable_data_id = td.data_id) '
        )
        columns_clause = (
            'l.market, l.seller, l.timestamp AS timestamp, l.listing_id, l.currency, '
            'l.sale_id, col.verified, l.maker, l.collection, l.price, ci.image as collection_image, '
            'cn.name AS collection_name, (SELECT usd FROM usd_prices ORDER BY timestamp DESC LIMIT 1) AS usd_wax, '
            '{badges_object}, {tags_obj}, {assets_object} '.format(
                badges_object=_get_badges_object('l.'),
                tags_obj=_get_tags_object('l.'),
                assets_object=_get_assets_object()
            )
        )
        group_clause = ' '
        if search_type == 'packs':
            search_clause += (
                ' AND EXISTS (SELECT template_id FROM packs p WHERE template_id = a.template_id) '
            )
        elif search_type == 'below_average':
            search_clause += (
                ' AND ts.avg_wax_price IS NOT NULL and l.estimated_wax_price < ts.avg_wax_price ')
        elif search_type == 'below_last_sold':
            search_clause += (
                ' AND ts.last_sold_wax IS NOT NULL and l.estimated_wax_price < ts.last_sold_wax ')
        elif search_type == 'floor':
            search_clause += (
                ' AND l.estimated_wax_price = fp.floor_price '
            )

        if verified == 'verified':
            search_clause += ' AND col.verified '
        elif verified == 'unverified':
            search_clause += (
                ' AND ((ac.verified IS NULL AND ac.blacklisted IS NULL) OR (NOT ac.verified AND NOT ac.blacklisted))'
            )
        elif verified == 'all':
            search_clause += ' AND (NOT ac.blacklisted OR ac.blacklisted IS NULL) '
        elif verified == 'blacklisted':
            search_clause += ' AND ac.blacklisted '

        search_clause = _add_mint_filter(min_mint, max_mint, search_clause, format_dict)
        search_clause = _add_recently_sold_filter(recently_sold, search_clause)

        if pfps_only:
            search_clause += (
                'AND p.asset_id IS NOT NULL '
            )

        if contract:
            format_dict['contract'] = contract
            search_clause += ' AND a.contract = :contract'

        if favorites:
            search_clause += ' AND f.user_name IS NOT NULL'

        if backed:
            search_clause += ' AND ba.amount IS NOT NULL '

        if min_price and max_price:
            search_clause += (
                ' AND estimated_wax_price BETWEEN :min_price AND :max_price '
            )
            format_dict['min_price'] = min_price
            format_dict['max_price'] = max_price
        elif min_price:
            search_clause += (
                ' AND estimated_wax_price >= :min_price '
            )
            format_dict['min_price'] = min_price
        elif max_price:
            search_clause += (
                ' AND estimated_wax_price <= :max_price '
            )
            format_dict['max_price'] = max_price
        if user:
            if search_type == 'bulk_buy':
                search_clause += ' AND l.seller != :user '
            elif search_type in ['missing', 'floor_missing']:
                with_clause = (
                    ', my_assets AS ( '
                    'SELECT template_id FROM '
                    '('
                    '   SELECT a.template_id FROM assets a WHERE NOT burned '
                    '   AND owner = :user AND a.template_id > 0 '
                    '   UNION '
                    '   SELECT a.template_id FROM listings l INNER JOIN assets a ON a.asset_id = l.asset_ids[1] '
                    '   WHERE seller = :user AND a.template_id > 0 ' 
                    '   UNION '
                    '   SELECT a.template_id FROM stakes rs '
                    '   INNER JOIN assets a USING(asset_id) '
                    '   WHERE staker = :user AND NOT burned AND a.template_id > 0'
                    ') a '
                    'GROUP BY 1) '
                    ', filtered_listings AS ('
                    'SELECT l.sale_id, l.market, l.seller, l.timestamp, l.listing_id, l.currency, col.verified, '
                    'l.maker, l.collection, l.price, l.collection, array_agg(asset_ids) AS assets '
                    'FROM listings l '
                    'LEFT JOIN assets a ON (asset_id = asset_ids[1]) '
                    'LEFT JOIN my_assets ma USING (template_id) '
                    'LEFT JOIN pfp_assets p ON (a.asset_id = p.asset_id) '
                    'LEFT JOIN backed_assets ba ON (a.asset_id = ba.asset_id) '
                    'LEFT JOIN collections col ON (col.collection = l.collection) '
                    'LEFT JOIN templates t ON (t.template_id = a.template_id) '
                    'LEFT JOIN template_stats ts ON (a.template_id = ts.template_id) '
                    'LEFT JOIN template_floor_prices_mv fp ON (fp.template_id = a.template_id) '
                    'LEFT JOIN names n ON (a.name_id = n.name_id) '
                    'WHERE a.template_id > 0 AND ma.template_id IS NULL '
                    'GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, estimated_wax_price '
                    '{group_clause} {order_clause} {limit_clause}) '
                )

                source_clause += (
                    'LEFT JOIN my_assets ma ON a.template_id = ma.template_id '
                )
                source_clause += '{} JOIN listing_prices_mv lp USING(sale_id) '.format(
                    'INNER' if order_by in ['offer', 'diff'] else 'LEFT'
                )
                if search_type == 'floor_missing':
                    search_clause += ' AND fp.floor_price = estimated_wax_price '
            elif search_type == 'owned_lower_mints':
                with_clause = (
                    ', my_assets AS ( '
                    '    SELECT template_id, MIN(mint) AS mint FROM '
                    '    ('
                    '        SELECT a1.template_id, MIN(mint) AS mint FROM assets a1 '
                    '        WHERE owner = :user AND a1.template_id IS NOT NULL AND a1.mint IS NOT NULl '
                    '        AND NOT burned GROUP BY 1 '
                    '        UNION '
                    '        SELECT a2.template_id, MIN(a2.mint) AS mint FROM listings a1 '
                    '        INNER JOIN assets a2 ON asset_id = asset_ids[1] '
                    '        WHERE seller = :user AND a2.template_id IS NOT NULL AND a2.mint IS NOT NULL '
                    '        AND NOT burned GROUP BY 1 ' 
                    '        UNION '
                    '        SELECT a1.template_id, MIN(mint) AS mint FROM stakes rs '
                    '        INNER JOIN assets a1 USING(asset_id) '
                    '        WHERE staker = :user AND a1.template_id IS NOT NULL AND a1.mint IS NOT NULL '
                    '        AND NOT burned GROUP BY 1 '
                    '   ) a '
                    'GROUP BY 1 ORDER BY 1 ASC) '
                    ', filtered_listings AS ('
                    'SELECT l.sale_id, l.market, l.seller, l.timestamp, l.listing_id, l.currency, col.verified, '
                    'l.maker, l.collection, l.price, l.collection, array_agg(asset_ids) AS assets '
                    'FROM listings l '
                    'LEFT JOIN assets a ON (asset_id = asset_ids[1]) '
                    'LEFT JOIN my_assets ma USING (template_id) '
                    'LEFT JOIN pfp_assets p ON (a.asset_id = p.asset_id) '
                    'LEFT JOIN backed_assets ba ON (a.asset_id = ba.asset_id) '
                    'LEFT JOIN collections col ON (col.collection = l.collection) '
                    'LEFT JOIN templates t ON (t.template_id = a.template_id) '
                    'LEFT JOIN template_stats ts ON (a.template_id = ts.template_id) '
                    'LEFT JOIN template_floor_prices_mv fp ON (fp.template_id = a.template_id) '
                    'LEFT JOIN names n ON (a.name_id = n.name_id) '
                    'WHERE a.mint < ma.mint {search_clause} '
                    'GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, estimated_wax_price '
                    '{group_clause} {order_clause} {limit_clause}) '
                )
        if market:
            format_dict['market'] = '{}'.format(market.lower().strip())
            search_clause += ' AND l.market = :market '
        if owner:
            format_dict['owner'] = '{}'.format(owner.lower().strip())
            if search_type == 'bulk_buy':
                search_clause += ' AND l.seller = :owner '
            elif search_type == 'my_exp_auctions':
                search_clause += ' AND a.owner = \'atomicmarket\' AND au.seller = :owner AND au.bidder IS NULL '
            elif search_type == 'my_auctions':
                search_clause += (
                    ' AND (a1.asset_id, a1.listing_id, a1.transaction_id) IN ('
                    '     SELECT asset_id, listing_id, transaction_id FROM auctions WHERE '
                    '     asset_id = a1.asset_id AND (bidder = :owner OR seller = :owner) '
                    ' ) '
                )
                search_clause += ' AND a1.mint = max_mint'
            else:
                search_clause += ' AND a1.seller = :search_owner '

        if order_by == 'rarity_score':
            order_clause = 'ORDER BY l.collection, h.schema, h.rarity_score {}'.format(order_dir)
            group_clause += ', l.collection, h.schema, h.rarity_score '
            search_clause += ' AND h.rarity_score IS NOT NULL '
        elif order_by == 'date':
            order_clause = 'ORDER BY l.seq {}'.format(order_dir)
        elif order_by == 'price':
            order_clause = 'ORDER BY estimated_wax_price {}'.format(order_dir)
            group_clause += ', estimated_wax_price '
        elif order_by == 'mint':
            group_clause += ', h.mint '
            search_clause += ' AND h.mint > 0 '
            order_clause = 'ORDER BY h.mint {}'.format(order_dir)
        elif order_by == 'template_id':
            search_clause += ' AND h.template_id IS NOT NULL '
            order_clause = (
                'ORDER BY h.template_id {}'
            ).format(order_dir)
            search_clause += ' AND h.template_id IS NOT NULL '
            group_clause += ', h.template_id '
        elif order_by == 'collection':
            order_clause = (
                'ORDER BY l.collection {}'
            ).format(order_dir)
        elif order_by == 'floor':
            search_clause += ' AND h.floor_price IS NOT NULL '
            order_clause = (
                'ORDER BY h.floor_price {}'
            ).format(order_dir)
            group_clause += ', h.floor_price '

        with_clause = with_clause.format(
            search_clause=search_clause,
            limit_clause=limit_clause,
            order_clause=order_clause,
            columns_clause=columns_clause,
            group_clause=group_clause
        )

        sql = (
            'WITH usd_rate AS (SELECT usd FROM usd_prices ORDER BY timestamp DESC LIMIT 1) '
            '{with_clause} '
            'SELECT {columns_clause}, f.user_name IS NOT NULL AS favorited '
            'FROM {source_clause} '
            '{join_clause} '
            '{personal_blacklist_clause} '
            'GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, l.timestamp, f.user_name {group_clause}'
            '{order_clause}'.format(
                with_clause=with_clause,
                columns_clause=columns_clause,
                source_clause=source_clause,
                group_clause=group_clause,
                order_clause=order_clause,
                limit_clause=limit_clause,
                join_clause=join_clause,
                personal_blacklist_clause=personal_blacklist_clause
            ))

        print(sql)

        res = session.execute(sql, format_dict)

        results = []

        for row in res:
            try:
                result = _format_listings(row)

                results.append(result)
            except Exception as e:
                logging.error(e)

        return results
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def health():
    session = create_session()
    try:
        result = session.execute(
            'SELECT block_num, timestamp FROM chronicle_transactions WHERE seq = ('
            'SELECT max(seq) FROM chronicle_transactions WHERE ingested)'
        ).first()

        if result:
            return {
                'success': 'true',
                'block_num': result['block_num'],
                'timestamp': result['timestamp'].strftime("%Y-%m-%d %H:%M:%S")
            }

        return {
            'success': 'false'
        }
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


@cache.memoize(timeout=3000)
def attribute_names(author, category):
    session = create_session()
    try:
        result = session.execute(
            'SELECT key, value, category '
            'FROM attribute_names an '
            'WHERE author = :author '
            'GROUP BY 1, 2, 3'.format(
                category_clause=' category = :category ' if category else ' category IS NULL '
            ), {'author': author, 'category': category}
        )

        names = {
            'variant': 'Variant',
            'rarity': 'Rarity',
            'number': 'Number',
            'type': 'Type',
            'color': 'Color',
            'border': 'Border',
            'attr7': '',
            'attr8': '',
            'attr9': '',
            'attr10': ''
        }

        default_names = names.copy()

        for row in result:
            if category and row['category'] and category == row['category']:
                names[row['key']] = row['value']
            elif category and not row['category'] and names[row['key']] == default_names[row['key']]:
                names[row['key']] = row['value']
            elif not category and not row['category']:
                names[row['key']] = row['value']

        return names
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def buyoffer(offer_id):
    session = create_session()
    try:
        buyoffer = session.execute(
            'SELECT timestamp, offer, currency, buyer, template_id, author, name, active, author_id, image, video '
            'FROM buyoffers '
            'WHERE offer_id = :offer_id',
            {'offer_id': offer_id}
        ).first()

        return {
            'timestamp': str(buyoffer['timestamp']),
            'offer': buyoffer['offer'],
            'currency': buyoffer['currency'],
            'buyer': buyoffer['buyer'],
            'template_id': buyoffer['template_id'],
            'author': buyoffer['author'],
            'name': buyoffer['name'],
            'active': buyoffer['active'],
            'author_id': buyoffer['author_id'],
            'image': _format_image(buyoffer['image']),
            'preview': _format_banner(buyoffer['image']) if buyoffer['image'] else None,
            'video': _format_video(buyoffer['video']),
        }
    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()


def active_buyoffers(buyer):
    session = create_session()
    query = (
        'SELECT timestamp, offer, currency, buyer, template_id, author, name, active, author_id, image, video ' 
        'FROM buyoffers ' 
        'WHERE active '
    )
    if buyer:
        query += 'AND buyer = :buyer'
    try:
        res = session.execute(query, {'buyer': buyer})
        buyoffers = []
        for buyoffer in res:
            buyoffers.append(
                {
                    'timestamp': str(buyoffer['timestamp']),
                    'offer': buyoffer['offer'],
                    'currency': buyoffer['currency'],
                    'buyer': buyoffer['buyer'],
                    'template_id': buyoffer['template_id'],
                    'author': buyoffer['author'],
                    'name': buyoffer['name'],
                    'active': buyoffer['active'],
                    'author_id': buyoffer['author_id'],
                    'image': buyoffer['image'],
                    'video': buyoffer['video'],
                }
            )
        return buyoffers

    except SQLAlchemyError as e:
        logging.error(e)
        session.rollback()
        raise e
    finally:
        session.remove()
