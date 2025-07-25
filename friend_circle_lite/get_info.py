import logging
from datetime import datetime, timedelta, timezone
import re
import os
import json
from urllib.parse import urljoin, urlparse
from dateutil import parser
from zoneinfo import ZoneInfo
import requests
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed

# 标准化的请求头
HEADERS_JSON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(Friend-Circle-Lite/1.0; +https://github.com/willow-god/Friend-Circle-Lite)"
    ),
    "X-Friend-Circle": "1.0"
}

HEADERS_XML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(Friend-Circle-Lite/1.0; +https://github.com/willow-god/Friend-Circle-Lite)"
    ),
    "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "X-Friend-Circle": "1.0"
}

timeout = (10, 15) # 连接超时和读取超时，防止requests接受时间过长

def format_published_time(time_str):
    """
    格式化发布时间为统一格式 YYYY-MM-DD HH:MM

    参数:
    time_str (str): 输入的时间字符串，可能是多种格式。

    返回:
    str: 格式化后的时间字符串，若解析失败返回空字符串。
    """
    # 尝试自动解析输入时间字符串
    try:
        parsed_time = parser.parse(time_str, fuzzy=True)
    except (ValueError, parser.ParserError):
        # 定义支持的时间格式
        time_formats = [
            '%a, %d %b %Y %H:%M:%S %z',  # Mon, 11 Mar 2024 14:08:32 +0000
            '%a, %d %b %Y %H:%M:%S GMT',   # Wed, 19 Jun 2024 09:43:53 GMT
            '%Y-%m-%dT%H:%M:%S%z',         # 2024-03-11T14:08:32+00:00
            '%Y-%m-%dT%H:%M:%SZ',          # 2024-03-11T14:08:32Z
            '%Y-%m-%d %H:%M:%S',           # 2024-03-11 14:08:32
            '%Y-%m-%d'                     # 2024-03-11
        ]
        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue
        else:
            logging.warning(f"无法解析时间字符串：{time_str}")
            return ''

    # 处理时区转换
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
    shanghai_time = parsed_time.astimezone(timezone(timedelta(hours=8)))
    return shanghai_time.strftime('%Y-%m-%d %H:%M')

def check_feed(blog_url, session):
    """
    检查博客的 RSS 或 Atom 订阅链接。

    优化点：
    - 检查 HTTP 状态码。
    - 检查 Content-Type 是否包含 xml / rss / atom。
    - 检查响应内容前几百字节内是否有 RSS/Atom 的特征标签。
    """
    possible_feeds = [
        ('atom', '/atom.xml'),
        ('rss', '/rss.xml'),  # 2024-07-26 添加 /rss.xml内容的支持
        ('rss2', '/rss2.xml'),
        ('rss3', '/rss.php'),  # 2024-12-07 添加 /rss.php内容的支持
        ('feed', '/feed'),
        ('feed2', '/feed.xml'),  # 2024-07-26 添加 /feed.xml内容的支持
        ('feed3', '/feed/'),
        ('feed4', '/feed.php'),  # 2025-07-22 添加 /feed.php内容的支持
        ('index', '/index.xml')  # 2024-07-25 添加 /index.xml内容的支持
    ]

    for feed_type, path in possible_feeds:
        feed_url = blog_url.rstrip('/') + path
        try:
            response = session.get(feed_url, headers=HEADERS_XML, timeout=timeout)
            if response.status_code == 200:
                # 检查 Content-Type
                content_type = response.headers.get('Content-Type', '').lower()
                if 'xml' in content_type or 'rss' in content_type or 'atom' in content_type:
                    return [feed_type, feed_url]
                
                # 如果 Content-Type 是 text/html 或未明确，但内容本身是 RSS
                text_head = response.text[:1000].lower()  # 读取前1000字符
                if ('<rss' in text_head or '<feed' in text_head or '<rdf:rdf' in text_head):
                    return [feed_type, feed_url]
        except requests.RequestException:
            continue

    logging.warning(f"无法找到 {blog_url} 的订阅链接")
    return ['none', blog_url]


def parse_feed(url, session, count=5, blog_url=''):
    """
    解析 Atom 或 RSS2 feed 并返回包含网站名称、作者、原链接和每篇文章详细内容的字典。

    此函数接受一个 feed 的地址（atom.xml 或 rss2.xml），解析其中的数据，并返回一个字典结构，
    其中包括网站名称、作者、原链接和每篇文章的详细内容。

    参数：
    url (str): Atom 或 RSS2 feed 的 URL。
    session (requests.Session): 用于请求的会话对象。
    count (int): 获取文章数的最大数。如果小于则全部获取，如果文章数大于则只取前 count 篇文章。

    返回：
    dict: 包含网站名称、作者、原链接和每篇文章详细内容的字典。
    """
    try:
        response = session.get(url, headers=HEADERS_XML, timeout=timeout)
        response.encoding = response.apparent_encoding or 'utf-8'
        feed = feedparser.parse(response.text)
        
        result = {
            'website_name': feed.feed.title if 'title' in feed.feed else '', # type: ignore
            'author': feed.feed.author if 'author' in feed.feed else '', # type: ignore
            'link': feed.feed.link if 'link' in feed.feed else '', # type: ignore
            'articles': []
        }
        
        for _ , entry in enumerate(feed.entries):
            
            if 'published' in entry:
                published = format_published_time(entry.published)
            elif 'updated' in entry:
                published = format_published_time(entry.updated)
                # 输出警告信息
                logging.warning(f"文章 {entry.title} 未包含发布时间，已使用更新时间 {published}")
            else:
                published = ''
                logging.warning(f"文章 {entry.title} 未包含任何时间信息, 请检查原文, 设置为默认时间")
            
            # 处理链接中可能存在的错误，比如ip或localhost
            article_link = replace_non_domain(entry.link, blog_url) if 'link' in entry else '' # type: ignore
            
            article = {
                'title': entry.title if 'title' in entry else '',
                'author': result['author'],
                'link': article_link,
                'published': published,
                'summary': entry.summary if 'summary' in entry else '',
                'content': entry.content[0].value if 'content' in entry and entry.content else entry.description if 'description' in entry else ''
            }
            result['articles'].append(article)
        
        # 对文章按时间排序，并只取前 count 篇文章
        result['articles'] = sorted(result['articles'], key=lambda x: datetime.strptime(x['published'], '%Y-%m-%d %H:%M'), reverse=True)
        if count < len(result['articles']):
            result['articles'] = result['articles'][:count]
        
        return result
    except Exception as e:
        logging.error(f"无法解析FEED地址：{url} ，请自行排查原因！")
        return {
            'website_name': '',
            'author': '',
            'link': '',
            'articles': []
        }

def replace_non_domain(link: str, blog_url: str) -> str:
    """
    暂未实现
    检测并替换字符串中的非正常域名部分（如 IP 地址或 localhost），替换为 blog_url。
    替换后强制使用 https，且考虑 blog_url 尾部是否有斜杠。

    :param link: 原始地址字符串
    :param blog_url: 替换为的博客地址
    :return: 替换后的地址字符串
    """
    
    # 提取link中的路径部分，无需协议和域名
    # path = re.sub(r'^https?://[^/]+', '', link)
    # print(path)
    
    try:
        parsed = urlparse(link)
        if 'localhost' in parsed.netloc or re.match(r'^\d{1,3}(\.\d{1,3}){3}$', parsed.netloc):  # IP地址或localhost
            # 提取 path + query
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            return urljoin(blog_url.rstrip('/') + '/', path.lstrip('/'))
        else:
            return link  # 合法域名则返回原链接
    except Exception as e:
        logging.warning(f"替换链接时出错：{link}, error: {e}")
        return link

def process_friend(friend, session, count, specific_and_cache=None):
    """
    处理单个朋友的博客信息。
    
    参数：
        friend (list/tuple): [name, blog_url, avatar]
        session (requests.Session): 请求会话
        count (int): 每个博客最大文章数
        specific_and_cache (list[dict]): [{name, url, source?}]，合并后的特殊 + 缓存列表
    
    返回：
        {
            'name': name,
            'status': 'active' | 'error',
            'articles': [...],
            'feed_url': str | None,
            'feed_type': str,
            'cache_update': {
                'action': 'set' | 'delete' | 'none',
                'name': name,
                'url': feed_url_or_None,
                'reason': 'auto_discovered' | 'repair_cache' | 'remove_invalid',
            },
            'source_used': 'manual' | 'cache' | 'auto' | 'none'
        }
    """
    if specific_and_cache is None:
        specific_and_cache = []

    # 解包 friend
    try:
        name, blog_url, avatar = friend
    except Exception:
        logging.error(f"friend 数据格式不正确: {friend!r}")
        return {
            'name': None,
            'status': 'error',
            'articles': [],
            'feed_url': None,
            'feed_type': 'none',
            'cache_update': {'action': 'none', 'name': None, 'url': None, 'reason': 'bad_friend_data'},
            'source_used': 'none',
        }

    rss_lookup = {e['name']: e for e in specific_and_cache if 'name' in e and 'url' in e}
    cache_update = {'action': 'none', 'name': name, 'url': None, 'reason': ''}
    feed_url, feed_type, source_used = None, 'none', 'none'

    # ---- 1. 优先使用 specific 或 cache ----
    entry = rss_lookup.get(name)
    if entry:
        feed_url = entry['url']
        feed_type = 'specific'
        source_used = entry.get('source', 'unknown')
        logging.info(f"“{name}” 使用预设 RSS 源：{feed_url} （source={source_used}）。")
    else:
        # ---- 2. 自动探测 ----
        feed_type, feed_url = check_feed(blog_url, session)
        source_used = 'auto'
        logging.info(f"“{name}” 自动探测 RSS：type：{feed_type}, url：{feed_url} 。")

        if feed_type != 'none' and feed_url:
            cache_update = {'action': 'set', 'name': name, 'url': feed_url, 'reason': 'auto_discovered'}

    # ---- 3. 尝试解析 RSS ----
    articles, parse_error = [], False
    if feed_type != 'none' and feed_url:
        try:
            feed_info = parse_feed(feed_url, session, count, blog_url)
            if isinstance(feed_info, dict) and 'articles' in feed_info:
                articles = [
                    {
                        'title': a['title'],
                        'created': a['published'],
                        'link': a['link'],
                        'author': name,
                        'avatar': avatar,
                    }
                    for a in feed_info['articles']
                ]

                for a in articles:
                    logging.info(f"{name} 发布了新文章：{a['title']}，时间：{a['created']}，链接：{a['link']}")
            else:
                parse_error = True
        except Exception as e:
            logging.warning(f"解析 RSS 失败（{name} -> {feed_url}）：{e}")
            parse_error = True

    # ---- 4. 如果缓存 RSS 无效则重新探测 ----
    if parse_error and source_used in ('cache', 'unknown'):
        logging.info(f"缓存 RSS 无效，重新探测：{name} ({blog_url})。")
        new_type, new_url = check_feed(blog_url, session)
        if new_type != 'none' and new_url:
            try:
                feed_info = parse_feed(new_url, session, count, blog_url)
                if isinstance(feed_info, dict) and 'articles' in feed_info:
                    articles = [
                        {
                            'title': a['title'],
                            'created': a['published'],
                            'link': a['link'],
                            'author': name,
                            'avatar': avatar,
                        }
                        for a in feed_info['articles']
                    ]

                    for a in articles:
                        logging.info(f"{name} 发布了新文章：{a['title']}，时间：{a['created']}，链接：{a['link']}")

                    feed_type, feed_url, source_used = new_type, new_url, 'auto'
                    cache_update = {'action': 'set', 'name': name, 'url': new_url, 'reason': 'repair_cache'}
                    parse_error = False
            except Exception as e:
                logging.warning(f"重新探测解析仍失败：{name} ({new_url})：{e}")
                cache_update = {'action': 'delete', 'name': name, 'url': None, 'reason': 'remove_invalid'}
                feed_type, feed_url = 'none', None
        else:
            cache_update = {'action': 'delete', 'name': name, 'url': None, 'reason': 'remove_invalid'}
            feed_type, feed_url = 'none', None

    # ---- 5. 最终状态 ----
    status = 'active' if articles else 'error'
    if not articles:
        if feed_type == 'none':
            logging.warning(f"{name} 的博客 {blog_url} 未找到有效 RSS。")
        else:
            logging.warning(f"{name} 的 RSS {feed_url} 未解析出文章。")

    return {
        'name': name,
        'status': status,
        'articles': articles,
        'feed_url': feed_url,
        'feed_type': feed_type,
        'cache_update': cache_update,
        'source_used': source_used,
    }

def _load_cache(cache_file):
    if not cache_file:
        return []
    if not os.path.exists(cache_file):
        logging.info(f"缓存文件 {cache_file} 不存在，将自动创建。")
        return []
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            logging.warning(f"缓存文件 {cache_file} 格式异常（应为列表）。将忽略。")
            return []
        # 标准化
        norm = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get('name')
            url = item.get('url')
            if name and url:
                norm.append({'name': name, 'url': url, 'source': 'cache'})
        return norm
    except Exception as e:
        logging.warning(f"读取缓存文件 {cache_file} 失败: {e}")
        return []

def _atomic_write_json(path, data) -> None:
    """原子写，减少写坏文件风险。"""
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _save_cache(cache_file, cache_items):
    if not cache_file:
        return
    try:
        # 丢弃 source 字段以保持文件简洁
        out = [{'name': i['name'], 'url': i['url']} for i in cache_items]
        _atomic_write_json(cache_file, out)
        logging.info(f"缓存已保存到 {cache_file}（{len(out)} 条）。")
    except Exception as e:
        logging.error(f"保存缓存文件 {cache_file} 失败: {e}")

def fetch_and_process_data(json_url, specific_RSS=None, count=5, cache_file=None):
    """
    读取 JSON 数据并处理订阅信息，返回统计数据和文章信息。

    参数：
        json_url (str): 包含朋友信息的 JSON 文件的 URL。
        count (int): 获取每个博客的最大文章数。
        specific_RSS (list): 包含特定 RSS 源的字典列表 [{name, url}]（来自 YAML）。
        cache_file (str): 缓存文件路径。

    返回：
        (result_dict, error_friends_info_list)
    """
    if specific_RSS is None:
        specific_RSS = []

    # 1. 加载缓存
    cache_list = _load_cache(cache_file)

    # 2. 标记 YAML 条目
    manual_list = []
    for item in specific_RSS:
        if isinstance(item, dict) and 'name' in item and 'url' in item:
            manual_list.append({'name': item['name'], 'url': item['url'], 'source': 'manual'})

    # 3. 合并（缓存先，YAML 后覆盖）
    combined_map = {e['name']: e for e in cache_list}
    for e in manual_list:  # 手动优先
        combined_map[e['name']] = e
    specific_and_cache = list(combined_map.values())

    # 4. 建立方便判断的集合：手动源名称集合
    manual_name_set = {e['name'] for e in manual_list}

    # 5. 获取朋友列表
    session = requests.Session()
    try:
        response = session.get(json_url, headers=HEADERS_JSON, timeout=timeout)
        friends_data = response.json()
    except Exception as e:
        logging.error(f"无法获取链接：{json_url} ：{e}", exc_info=True)
        return None

    friends = friends_data.get('friends', [])
    total_friends = len(friends)
    active_friends = 0
    error_friends = 0
    total_articles = 0
    article_data = []
    error_friends_info = []
    cache_updates = []  # 用于收集缓存更新（线程安全：用局部列表 + 合并）

    # 6. 并发处理
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_friend = {
            executor.submit(process_friend, friend, session, count, specific_and_cache): friend
            for friend in friends
        }

        for future in as_completed(future_to_friend):
            friend = future_to_friend[future]
            try:
                result = future.result()

                # 拿回缓存更新意图
                upd = result.get('cache_update', {})
                if upd and upd.get('action') != 'none':
                    cache_updates.append(upd)

                if result['status'] == 'active':
                    active_friends += 1
                    article_data.extend(result['articles'])
                    total_articles += len(result['articles'])
                else:
                    error_friends += 1
                    error_friends_info.append(friend)

            except Exception as e:
                logging.error(f"处理 {friend} 时发生错误: {e}", exc_info=True)
                error_friends += 1
                error_friends_info.append(friend)

    # 7. 处理缓存更新
    cache_map = {e['name']: e for e in cache_list}

    # 去重 & 过滤无效条目
    unique_updates = {}
    for upd in cache_updates:
        name = upd.get('name')
        action = upd.get('action')
        url = upd.get('url')
        if not name:
            continue

        # 过滤手动 YAML 的条目（不允许覆盖）
        if name in manual_name_set:
            continue

        # 只缓存有效 RSS 地址
        if action == 'set':
            if url and url != 'none' and url != '':
                unique_updates[name] = {'action': 'set', 'url': url, 'reason': upd.get('reason', '')}
        elif action == 'delete':
            unique_updates[name] = {'action': 'delete', 'url': None, 'reason': upd.get('reason', '')}

    # 应用缓存更新
    for name, upd in unique_updates.items():
        if upd['action'] == 'set':
            cache_map[name] = {'name': name, 'url': upd['url'], 'source': 'cache'}
            logging.info(f"缓存更新：SET {name} -> {upd['url']} ({upd['reason']})")
        elif upd['action'] == 'delete':
            if name in cache_map:
                cache_map.pop(name)
                logging.info(f"缓存更新：DELETE {name} ({upd['reason']})")

    # 8. 保存缓存
    _save_cache(cache_file, list(cache_map.values()))

    # 9. 汇总统计
    result = {
        'statistical_data': {
            'friends_num': total_friends,
            'active_num': active_friends,
            'error_num': error_friends,
            'article_num': total_articles,
            'last_updated_time': datetime.now(ZoneInfo("Asia/Shanghai")).strftime('%Y-%m-%d %H:%M:%S'),
        },
        'article_data': article_data,
    }

    logging.info(
        f"数据处理完成，总共有 {total_friends} 位朋友，其中 {active_friends} 位博客可访问，"
        f"{error_friends} 位博客无法访问。缓存更新 {len(unique_updates)} 条。"
    )

    return result, error_friends_info

def sort_articles_by_time(data):
    """
    对文章数据按时间排序

    参数：
    data (dict): 包含文章信息的字典

    返回：
    dict: 按时间排序后的文章信息字典
    """
    # 先确保每个元素存在时间
    for article in data['article_data']:
        if article['created'] == '' or article['created'] == None:
            article['created'] = '2024-01-01 00:00'
            # 输出警告信息
            logging.warning(f"文章 {article['title']} 未包含时间信息，已设置为默认时间 2024-01-01 00:00")
    
    if 'article_data' in data:
        sorted_articles = sorted(
            data['article_data'],
            key=lambda x: datetime.strptime(x['created'], '%Y-%m-%d %H:%M'),
            reverse=True
        )
        data['article_data'] = sorted_articles
    return data

def marge_data_from_json_url(data, marge_json_url):
    """
    从另一个 JSON 文件中获取数据并合并到原数据中。

    参数：
    data (dict): 包含文章信息的字典
    marge_json_url (str): 包含另一个文章信息的 JSON 文件的 URL。

    返回：
    dict: 合并后的文章信息字典，已去重处理
    """
    try:
        response = requests.get(marge_json_url, headers=HEADERS_JSON, timeout=timeout)
        marge_data = response.json()
    except Exception as e:
        logging.error(f"无法获取链接：{marge_json_url}，出现的问题为：{e}", exc_info=True)
        return data
    
    if 'article_data' in marge_data:
        logging.info(f"开始合并数据，原数据共有 {len(data['article_data'])} 篇文章，第三方数据共有 {len(marge_data['article_data'])} 篇文章")
        data['article_data'].extend(marge_data['article_data'])
        data['article_data'] = list({v['link']:v for v in data['article_data']}.values())
        logging.info(f"合并数据完成，现在共有 {len(data['article_data'])} 篇文章")
    return data

import requests

def marge_errors_from_json_url(errors, marge_json_url):
    """
    从另一个网络 JSON 文件中获取错误信息并遍历，删除在errors中，
    不存在于marge_errors中的友链信息。

    参数：
    errors (list): 包含错误信息的列表
    marge_json_url (str): 包含另一个错误信息的 JSON 文件的 URL。

    返回：
    list: 合并后的错误信息列表
    """
    try:
        response = requests.get(marge_json_url, timeout=10)  # 设置请求超时时间
        marge_errors = response.json()
    except Exception as e:
        logging.error(f"无法获取链接：{marge_json_url}，出现的问题为：{e}", exc_info=True)
        return errors

    # 提取 marge_errors 中的 URL
    marge_urls = {item[1] for item in marge_errors}

    # 使用过滤器保留 errors 中在 marge_errors 中出现的 URL
    filtered_errors = [error for error in errors if error[1] in marge_urls]

    logging.info(f"合并错误信息完成，合并后共有 {len(filtered_errors)} 位朋友")
    return filtered_errors

def deal_with_large_data(result):
    """
    处理文章数据，保留前150篇及其作者在后续文章中的出现。
    
    参数：
    result (dict): 包含统计数据和文章数据的字典。
    
    返回：
    dict: 处理后的数据，只包含需要的文章。
    """
    result = sort_articles_by_time(result)
    article_data = result.get("article_data", [])

    # 检查文章数量是否大于 150
    max_articles = 150
    if len(article_data) > max_articles:
        logging.info("数据量较大，开始进行处理...")
        # 获取前 max_articles 篇文章的作者集合
        top_authors = {article["author"] for article in article_data[:max_articles]}

        # 从第 {max_articles + 1} 篇开始过滤，只保留前 max_articles 篇出现过的作者的文章
        filtered_articles = article_data[:max_articles] + [
            article for article in article_data[max_articles:]
            if article["author"] in top_authors
        ]

        # 更新结果中的 article_data
        result["article_data"] = filtered_articles
        # 更新结果中的统计数据
        result["statistical_data"]["article_num"] = len(filtered_articles)
        logging.info(f"数据处理完成，保留 {len(filtered_articles)} 篇文章")

    return result