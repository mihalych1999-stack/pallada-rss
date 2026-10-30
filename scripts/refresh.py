"""Build a validated Pages RSS artifact; deploy only after this succeeds."""
import hashlib
import io
import json
import re
import urllib.error
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image
import rss_feed as rss

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'public'
FEED_ORIGIN='https://rss.pallada-n.ru'

def preserve_previous_media(output, fetcher=rss.fetch):
    """Retain one previous feed's media while readers may cache its XML."""
    try:
        previous=ET.fromstring(fetcher(FEED_ORIGIN+'/dzen-rss.xml'))
    except urllib.error.HTTPError as error:
        if error.code==404:return 0
        raise
    urls=set()
    for item in previous.findall('./channel/item'):
        enclosure=item.find('enclosure')
        if enclosure is not None:urls.add(enclosure.get('url',''))
        body=item.findtext('{'+rss.CONTENT+'}encoded','')
        urls.update(n.attrs.get('src','') for n in rss.Tree(body).root.walk() if n.tag=='img')
    if len(urls)>100:raise ValueError('Previous feed has too many images')
    copied=0
    for url in sorted(urls):
        u=urlsplit(url)
        if u.scheme!='https' or u.netloc!='rss.pallada-n.ru' or u.query or u.fragment or not re.fullmatch(r'/dzen-media/[a-f0-9]{24}\.jpg',u.path):raise ValueError('Invalid previous media URL')
        target=output/u.path.lstrip('/')
        if target.exists():continue
        raw=fetcher(url)
        image=Image.open(io.BytesIO(raw));image.load()
        if image.format!='JPEG' or image.width<700:raise ValueError('Invalid previous media bytes')
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw);copied+=1
    return copied

def main():
    ledger={'articles': [{'source': 'https://pallada-n.ru/novosti/pozharotushenie-stellazhnogo-sklada-esfr-adler', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/monolitnyy-ochistnoy-korpus-kruglye-steny-sirius', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/elektrosnabzhenie-rassadnogo-kompleksa-upravlyaemaya-dosvetka-sochi', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/hammam-gostinichnogo-kompleksa-paroizolyacionnyy-kontur-khosta', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/vodosnabzhenie-pishchevogo-predpriyatiya-ultrafiltraciya-adler', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/krovlya-obshchestvennogo-korpusa-kontrol-vlazhnosti-sirius', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/sauna-turisticheskogo-kompleksa-sbornaya-naruzhnaya-kabina-dagomys', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/gazofikaciya-fondohranilishcha-parouvlazhnenie-sirius', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/basseyn-spa-optovolokonnaya-podsvetka-dagomys', 'status': 'published'}, {'source': 'https://pallada-n.ru/novosti/vodootvedenie-laboratorii-neytralizaciya-stokov-sochi', 'status': 'published'}]}
    excluded=rss.published_exclusions(ledger)
    assert len(excluded)==10
    articles,_=rss.live_articles(20,ledger)
    overrides={}
    OUTPUT.mkdir(exist_ok=True)
    media=OUTPUT/'dzen-media'
    media.mkdir(exist_ok=True)
    for a in articles:
        urls={a.get('imageUrl','')}
        urls.update(n.attrs.get('src','') for n in rss.Tree(a['bodyHtml']).root.walk() if n.tag=='img')
        for url in sorted(urls-{''}):
            if url in overrides:continue
            raw=rss.fetch(url)
            image=Image.open(io.BytesIO(raw))
            image.load()
            if image.width<700 or image.width*image.height>4_000_000:raise ValueError('Unsupported image dimensions')
            name=hashlib.sha256(raw).hexdigest()[:24]+'.jpg'
            target=media/name
            image.convert('RGB').save(target,'JPEG',quality=95,optimize=True)
            with Image.open(target) as check:
                assert check.format=='JPEG' and check.size==image.size
            overrides[url]=FEED_ORIGIN+'/dzen-media/'+name
    xml,report=rss.build_feed(articles,ledger,20,overrides)
    assert len(report['items'])==20 and not excluded.intersection(x['source'] for x in report['items'])
    previous_media=preserve_previous_media(OUTPUT)
    (OUTPUT/'dzen-rss.xml').write_bytes(xml)
    (OUTPUT/'CNAME').write_text('rss.pallada-n.ru\n')
    (OUTPUT/'.nojekyll').touch()
    (OUTPUT/'robots.txt').write_text('User-agent: *\nAllow: /\n')
    (OUTPUT/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Паллада RSS</title><a href="dzen-rss.xml">RSS Паллады</a>')
    print(json.dumps({'items':len(report['items']),'initialManualExcluded':len(excluded),'images':len(overrides),'previousMediaRetained':previous_media,'mode':'native-draft','sha256':hashlib.sha256(xml).hexdigest()}))

if __name__=='__main__':main()
