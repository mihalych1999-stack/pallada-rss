#!/usr/bin/env python3
"""Build a LOCAL full-text Dzen RSS preview; never publish or modify Tilda.

Default reads the current public discovery RSS and /novosti, then the article
pages. --manifest uses a frozen source-articles.json without network access.
Only confirmed `published` entries in --ledger are excluded. Uncertain sends
block the build, preventing a later RSS import from duplicating an unknown send.
All items use native-draft. This script neither hosts nor connects the result.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import time
import http.client
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = 'https://pallada-n.ru'
CONTENT = 'http://purl.org/rss/1.0/modules/content/'
DEFAULT_LEDGER = ROOT / 'qa/dzen-setup-20261028/batch.json'
MIN_ITEMS, MAX_ITEMS = 10, 100
ALLOWED = {'p', 'a', 'b', 'i', 'u', 's', 'h1', 'h2', 'h3', 'h4',
           'blockquote', 'ul', 'ol', 'li', 'figure', 'figcaption', 'img'}
VOID = {'img', 'br', 'meta', 'link', 'input', 'hr', 'source', 'wbr'}


def canonical_url(value):
    parts = urlsplit(str(value))
    if parts.scheme != 'https' or parts.netloc != 'pallada-n.ru':
        raise ValueError('foreign_or_noncanonical_source_url: ' + str(value))
    if not re.fullmatch(r'/novosti/[a-z0-9][a-z0-9-]*', parts.path):
        raise ValueError('article_route_required: ' + str(value))
    if re.fullmatch(r'/novosti/\d+', parts.path):
        raise ValueError('pagination_is_not_article')
    if parts.query or parts.fragment:
        raise ValueError('source_url_has_tracking_or_fragment')
    return urlunsplit(parts)


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'PalladaRSSPreview/1.0'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if urlsplit(response.geturl()).scheme != 'https':
                    raise ValueError('insecure_redirect')
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {500, 502, 503, 504} or attempt == 2:
                raise
        except (TimeoutError, ConnectionError, urllib.error.URLError, http.client.IncompleteRead):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class Tree(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text_content(node):
    if isinstance(node, str):
        return node
    if node.tag in {'script', 'style'} or node.attrs.get('aria-hidden') == 'true':
        return ''
    return ''.join(text_content(c) for c in node.children)


def has_class(node, name):
    return name in node.attrs.get('class', '').split()


def supported_image(url):
    """A .jpg.webp resource is WebP, never a JPEG by stripping suffixes."""
    parts = urlsplit(url)
    return (parts.scheme == 'https' and bool(parts.netloc) and
            bool(re.search(r'\.(?:jpe?g|png|gif)$', parts.path, re.I)))


def mime_for(url):
    ext = Path(urlsplit(url).path).suffix.lower()
    return {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.gif': 'image/gif'}[ext]


def render(node, issues, replacements):
    if isinstance(node, str):
        return html.escape(node, quote=False)
    if node.tag in {'script', 'style', 'nav', 'aside', 'button'} or node.attrs.get('aria-hidden') == 'true':
        return ''
    tag = {'strong': 'b', 'em': 'i', 'summary': 'h3'}.get(node.tag, node.tag)
    if tag == 'br':
        return ' '
    body = ''.join(render(c, issues, replacements) for c in node.children)
    if tag == 'img':
        original = node.attrs.get('src', '')
        url = replacements.get(original, original)
        if not supported_image(url):
            issues.append({'code': 'unsupported_image_format', 'url': original})
            return ''
        attrs = {'src': url}
        for key in ('alt', 'width', 'height'):
            if node.attrs.get(key):
                attrs[key] = node.attrs[key]
        width = int(attrs.get('width', '0')) if str(attrs.get('width', '')).isdigit() else 0
        if width < 700:
            issues.append({'code': 'image_width_not_verified_700', 'url': url})
        return '<img' + ''.join(' ' + k + '="' + html.escape(v, quote=True) + '"' for k, v in attrs.items()) + '>'
    if tag not in ALLOWED:
        if tag in {'table', 'iframe', 'video', 'audio'}:
            issues.append({'code': 'unsupported_content_element', 'tag': tag})
        return body
    if tag == 'figure' and not body.strip():
        return ''
    attrs = ''
    if tag == 'a':
        url = node.attrs.get('href', '')
        if urlsplit(url).scheme not in {'https', 'http'} and not url.startswith('#'):
            return body
        attrs = ' href="' + html.escape(url, quote=True) + '"'
    elif tag in {'h2', 'h3', 'h4'} and node.attrs.get('id'):
        attrs = ' id="' + html.escape(node.attrs['id'], quote=True) + '"'
    return '<' + tag + attrs + '>' + body + '</' + tag + '>'


def article_from_page(url, metadata, raw):
    tree = Tree(raw.decode('utf-8')).root
    nodes = list(tree.walk())
    canonical = [n.attrs.get('href') for n in nodes if n.tag == 'link' and n.attrs.get('rel') == 'canonical']
    if canonical != [url]:
        raise ValueError('canonical_mismatch: ' + url)
    articles = [n for n in nodes if n.tag == 'article' and has_class(n, 'page')]
    if len(articles) != 1:
        raise ValueError('unique_pallada_article_required: ' + url)
    inside = list(articles[0].walk())
    titles = [text_content(n).strip() for n in inside if n.tag == 'h1']
    story = [n for n in inside if has_class(n, 'story')]
    faq = [n for n in inside if n.tag == 'section' and has_class(n, 'faq')]
    if len(titles) != 1 or len(story) != 1:
        raise ValueError('article_title_or_full_story_missing: ' + url)
    # Serialize only editorial story+FAQ; omit navigation and contact UI.
    def serialize(n):
        if isinstance(n, str):
            return html.escape(n, quote=False)
        attrs = ''.join(' ' + k + '="' + html.escape(v or '', quote=True) + '"' for k, v in n.attrs.items())
        return '<' + n.tag + attrs + '>' + ''.join(serialize(c) for c in n.children) + ('' if n.tag in VOID else '</' + n.tag + '>')
    body = serialize(story[0]) + ''.join(serialize(n) for n in faq)
    return {**metadata, 'url': url, 'title': titles[0], 'bodyHtml': body,
            'canonicalVerified': True, 'sourcePageSha256': hashlib.sha256(raw).hexdigest()}


