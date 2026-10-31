from rss_core import *

def published_exclusions(ledger):
    excluded, unresolved = set(), []
    for item in (ledger or {}).get('articles', []):
        source = item.get('source') or item.get('sourceUrl') or item.get('url')
        if not source:
            continue
        status = str(item.get('status', '')).lower()
        if status == 'published':
            excluded.add(canonical_url(source))
        elif status in {'publishing', 'uncertain', 'sending', 'submit_pending', 'awaiting_owner_captcha'}:
            unresolved.append(source)
    if unresolved:
        raise ValueError('unresolved_manual_publication: ' + ', '.join(unresolved))
    return excluded


def select_articles(articles, ledger=None, limit=100):
    if not MIN_ITEMS <= limit <= MAX_ITEMS:
        raise ValueError('limit_must_be_10_to_100')
    excluded = published_exclusions(ledger)
    seen, selected = set(), []
    # Input order is authoritative frozen /novosti or current source RSS order.
    for original in articles:
        source = canonical_url(original.get('url') or original.get('source'))
        if source in seen:
            continue
        seen.add(source)
        if source in excluded:
            continue
        selected.append(copy.deepcopy(original))
        if len(selected) == limit:
            break
    if len(selected) < MIN_ITEMS:
        raise ValueError('fewer_than_10_unpublished_materials: ' + str(len(selected)))
    return selected, sorted(excluded)


def build_feed(articles, ledger=None, limit=100, image_overrides=None):
    selected, excluded = select_articles(articles, ledger, limit)
    overrides = image_overrides or {}
    rows, item_xml = [], []
    for original in selected:
        source = canonical_url(original['url'])
        if original.get('canonicalVerified') is not True:
            raise ValueError('canonical_not_verified: ' + source)
        title = str(original.get('title', '')).strip()
        raw = original.get('bodyHtml', '')
        if not title or not raw:
            raise ValueError('title_or_full_body_missing: ' + source)
        issues = []
        body = render(Tree(raw).root, issues, overrides)
        plain = re.sub(r'\s+', ' ', text_content(Tree(body).root)).strip()
        if len(plain) < 300:
            raise ValueError('full_text_too_short: ' + source)
        published = parsedate_to_datetime(original['publishedAtRss'])
        if published.tzinfo is None:
            raise ValueError('publication_timezone_missing')
        guid = str(original.get('rssGuid') or source)
        cover = overrides.get(original.get('imageUrl', ''), original.get('imageUrl', ''))
        enclosure = ''
        if cover and supported_image(cover):
            enclosure = '<enclosure url="' + html.escape(cover, quote=True) + '" type="' + mime_for(cover) + '"/>'
            issues.append({'code': 'image_delivery_and_dimensions_require_verification', 'url': cover})
        elif cover:
            issues.append({'code': 'unsupported_cover_format', 'url': cover})
        if not enclosure:
            issues.append({'code': 'cover_missing_or_unsupported'})
        # Keep original GUID/source stable; never use Dzen draft IDs as GUIDs.
        desc = str(original.get('rssDescription', ''))
        item_xml.append('<item><title>' + html.escape(title) + '</title><link>' + html.escape(source) +
                        '</link><guid isPermaLink="' + ('true' if guid == source else 'false') + '">' + html.escape(guid) +
                        '</guid><pubDate>' + format_datetime(published) + '</pubDate><description>' + html.escape(desc) +
                        '</description><category>format-article</category><category>native-draft</category>' + enclosure +
                        '<content:encoded><![CDATA[' + body.replace(']]>', ']]]]><![CDATA[>') + ']]></content:encoded></item>')
        rows.append({'source': source, 'guid': guid, 'fullTextCharacters': len(plain),
                     'contentSha256': hashlib.sha256(body.encode()).hexdigest(), 'issues': issues})
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:content="' + CONTENT +
           '"><channel><title>Паллада — строительство и инженерные системы</title><link>' + SITE +
           '</link><language>ru</language><description>Полные статьи с сайта Паллады.</description>' +
           '\n'.join(item_xml) + '</channel></rss>\n').encode()
    parsed = ET.fromstring(xml)
    if len(parsed.findall('./channel/item')) != len(rows):
        raise ValueError('serialized_item_count_mismatch')
    return xml, {'status': 'LOCAL_PREVIEW_ONLY', 'publicationReady': False,
                 'mode': 'native-draft', 'itemCount': len(rows), 'excludedPublishedSources': excluded,
                 'imageVerification': 'Declared URLs only; no extension rewriting or content-type claims from WebP',
                 'hostingConfigured': False, 'dzenConnected': False,
                 'feedSha256': hashlib.sha256(xml).hexdigest(), 'items': rows,
                 'deploymentBlockers': ['Public same-domain HTTPS endpoint not deployed',
                                        'Dzen domain confirmation / 10 subscribers / moderation must be verified'] +
                                       (['Unsupported or unverified images/content; see item issues'] if any(r['issues'] for r in rows) else []),
                 'officialSources': ['https://dzen.ru/help/ru/website/site-to-channel.html',
                                     'https://dzen.ru/help/ru/website/rss-modify.html']}


def live_articles(limit, ledger):
    rss_bytes = fetch(SITE + '/rss.xml')
    feed = ET.fromstring(rss_bytes)
    news_bytes = fetch(SITE + '/novosti')
    tree = Tree(news_bytes.decode()).root
    news_urls = []
    for node in tree.walk():
        if node.tag != 'a':
            continue
        try:
            source = canonical_url(urljoin(SITE + '/novosti', node.attrs.get('href', '')))
        except ValueError:
            continue
        if source not in news_urls:
            news_urls.append(source)
    candidates = []
    for item in feed.findall('./channel/item'):
        source = item.findtext('link', '')
        try:
            canonical_url(source)
        except ValueError:
            continue
        enclosure = item.find('enclosure')
        candidates.append({'url': source, 'title': item.findtext('title', ''),
                           'rssGuid': item.findtext('guid', '') or source,
                           'publishedAtRss': item.findtext('pubDate', ''),
                           'rssDescription': item.findtext('description', ''),
                           'imageUrl': enclosure.get('url', '') if enclosure is not None else ''})
    # Ordinary-page RSS excludes native Feeds posts. Preserve its consistency
    # gate for the ordinary subset; the visible news order joins both routes.
    ordinary = [u for u in news_urls if '/novosti/' in u]
    compare = min(10, len(ordinary), len(candidates))
    if compare < 10 or ordinary[:compare] != [a['url'] for a in candidates[:compare]]:
        raise ValueError('live_rss_and_novosti_first10_disagree')
    by_url = {a['url']: a for a in candidates}
    cache = {}
    merged = []
    for source in news_urls:
        if '/tpost/' not in source:
            if source not in by_url:
                raise ValueError('ordinary_news_missing_from_rss: ' + source)
            merged.append(by_url[source])
            continue
        raw = fetch(source)
        cache[source] = raw
        nodes = list(Tree(raw.decode()).root.walk())
        metadata = []
        for n in nodes:
            if n.tag == 'script' and n.attrs.get('type') == 'application/ld+json':
                try:
                    value = json.loads(''.join(c for c in n.children if isinstance(c, str)))
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and value.get('@type') == 'Article' and value.get('mainEntityOfPage') == source:
                    metadata.append(value)
        if len(metadata) != 1:
            raise ValueError('unique_native_article_metadata_required: ' + source)
        data = metadata[0]
        published = datetime.fromisoformat(data['datePublished'])
        if published.tzinfo is None:
            raise ValueError('native_publication_timezone_missing')
        merged.append({'url': source, 'title': data['headline'], 'rssGuid': source,
                       'publishedAtRss': format_datetime(published),
                       'rssDescription': data.get('description', ''), 'imageUrl': data['image']})
        if len(merged) >= limit + len(published_exclusions(ledger)):
            break
    selected, _ = select_articles(merged, ledger, limit)
    return [article_from_page(a['url'], a, cache[a['url']] if a['url'] in cache else fetch(a['url'])) for a in selected], {
        'rssSha256': hashlib.sha256(rss_bytes).hexdigest(),
        'newsSha256': hashlib.sha256(news_bytes).hexdigest(), 'source': 'live_public_read_only'}

