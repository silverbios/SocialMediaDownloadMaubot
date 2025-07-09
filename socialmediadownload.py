import re
import json
import mimetypes
import instaloader
import urllib
import yarl
import requests
import asyncio
import concurrent.futures
from urllib.parse import urljoin

from typing import Type
from urllib.parse import quote
from mautrix.types import ImageInfo, EventType, MessageType
from mautrix.types.event.message import BaseFileInfo, Format, TextMessageEventContent
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, MessageEvent
from maubot.handlers import event

class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        for prefix in ["reddit", "instagram", "youtube", "tiktok"]:
            for suffix in ["enabled", "info", "image", "video", "thumbnail"]:
                helper.copy(f"{prefix}.{suffix}")

        helper.copy("respond_to_notice")

reddit_pattern = re.compile(r"((?:https?:)?\/\/)?((?:www|m|old|nm)\.)?((?:reddit\.com|redd\.it))(\/r\/[^/]+\/(?:comments|s)\/[a-zA-Z0-9_\-]+)")
instagram_pattern = re.compile(r"(?:https?:\/\/)?(?:www\.)?instagram\.com\/?([a-zA-Z0-9\.\_\-]+)?\/([p]+)?([reel]+)?([tv]+)?([stories]+)?\/([a-zA-Z0-9\-\_\.]+)\/?([0-9]+)?")
youtube_pattern = re.compile(r"((?:https?:)?\/\/)?((?:www|m)\.)?((?:youtube\.com|youtu\.be))(\/(?:[\w\-]+\?v=|embed\/|v\/)?)([\w\-]+)(\S+)?")
tiktok_pattern = re.compile(r"((?:https?:)?\/\/)?((?:www|m|vm)\.)?((?:tiktok\.com))(\/[@a-zA-Z0-9\-\_\.]+)?(\/video\/)?([a-zA-Z0-9\-\_]+)?")
bluesky_pattern = re.compile(r"((?:https?:)?\/\/)?((?:www|bsky)\.)?((?:bsky\.app))(\/profile\/[a-zA-Z0-9\-\_\.]+)(\/post\/[a-zA-Z0-9\-\_]+)")

class SocialMediaDownloadPlugin(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, evt: MessageEvent) -> None:
        if (evt.content.msgtype != MessageType.TEXT and
        not (self.config["respond_to_notice"] and evt.content.msgtype == MessageType.NOTICE) or
        evt.content.body.startswith("!")):
            return


        for url_tup in youtube_pattern.findall(evt.content.body):
            await evt.mark_read()
            if self.config["youtube.enabled"]:
                await self.handle_youtube(evt, url_tup)

        for url_tup in instagram_pattern.findall(evt.content.body):
            await evt.mark_read()
            if self.config["instagram.enabled"] and url_tup[5]:
                await self.handle_instagram(evt, url_tup)

        for url_tup in reddit_pattern.findall(evt.content.body):
            await evt.mark_read()
            if self.config["reddit.enabled"]:
                await self.handle_reddit(evt, url_tup)

        for url_tup in tiktok_pattern.findall(evt.content.body):
            await evt.mark_read()
            if self.config["tiktok.enabled"]:
                await self.handle_tiktok(evt, url_tup)
        
        if self.config["bluesky.enabled"]:
            for url_tup in bluesky_pattern.findall(evt.content.body):
                await evt.mark_read()
                await self.handle_bluesky(evt, url_tup)

    async def get_ttdownloader_params(self, tokensDict, url) -> list:
        cookies = {
            'PHPSESSID': tokensDict['PHPSESSID']
        }
        headers = {
            'authority': 'ttdownloader.com',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://ttdownloader.com',
            'referer': 'https://ttdownloader.com/',
            'sec-ch-ua': '"Not?A_Brand";v="8", "Chromium";v="108", "Google Chrome";v="108"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
            'x-requested-with': 'XMLHttpRequest',
        }
        data = {
            'url': url,
            'format': '',
            'token': tokensDict['token'],
        }
        return cookies, headers, data
        
    def get_ttdownloader_tokens(self) -> dict:
        tokens = {}
        response = requests.get('https://ttdownloader.com/')
        if response.status_code != 200:
            self.log.warning(f"Unexpected status fetching tokens for ttdownloader.com: {response.status_code}")
            return
        
        token_match = re.search(r'<input type="hidden" id="token" name="token" value="([^"]+)"', response.text)
        token = token_match.group(1) if token_match else None
        tokens["token"] = token

        for cookie in response.cookies:
            tokens[cookie.name] = cookie.value
        
        return tokens

    async def handle_tiktok(self, evt, url_tup):  
        url = ''.join(url_tup)

        if self.config["tiktok.video"]:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                tokensDict = await loop.run_in_executor(
                pool, self.get_ttdownloader_tokens)

            cookies, headers, data = await self.get_ttdownloader_params(tokensDict, url)
            response = await self.http.post('https://ttdownloader.com/search/',cookies=cookies, headers=headers, data=data)
            
            if response.status != 200:
                self.log.warning(f"Unexpected status sending download request to ttdownloader.com: {response.status}")
                return
            
            href_values = re.findall(r'href="([^"]+)"', await response.text())
            valid_urls = [url for url in href_values if yarl.URL(url).scheme in ['http', 'https']]
            response = await self.http.get(valid_urls[0])
            
            if response.status != 200:
                self.log.warning(f"Unexpected status fetching video for TikTok URL {url_tup}: {response.status}")
                return

            media = await response.read()
            mime_type = 'video/mp4'
            file_extension = ".mp4"
            file_name = str(hash(url)) + file_extension
            uri = await self.client.upload_media(media, mime_type=mime_type, filename=file_name)
            await self.client.send_file(evt.room_id, url=uri, info=BaseFileInfo(mimetype=mime_type, size=len(media)), file_name=file_name, file_type=MessageType.VIDEO)     

    async def get_youtube_video_id(self, url):
        if "youtu.be" in url:
            video_id = url.split("youtu.be/")[1]
        else:
            video_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)['v'][0]
        return video_id.split("?", 1)[0]

    async def generate_youtube_query_url(self, url):
        params = {"format": "json", "url": url}
        query_url = "https://www.youtube.com/oembed"
        query_string = urllib.parse.urlencode(params)
        return f"{query_url}?{query_string}"

    async def handle_youtube(self, evt, url_tup):
        url = ''.join(url_tup)
        video_id = await self.get_youtube_video_id(url)

        query_url = await self.generate_youtube_query_url(url)
        response = await self.http.get(query_url)
        if response.status != 200:
            self.log.warning(f"Unexpected status fetching video title {query_url}: {response.status}")
            return

        response_text = await response.read()
        data = json.loads(response_text.decode())

        if self.config["youtube.info"]:
            await evt.reply(data['title'])

        if self.config["youtube.thumbnail"]:
            thumbnail_link = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            response = await self.http.get(thumbnail_link)
            if response.status != 200:
                self.log.warning(f"Unexpected status fetching image {thumbnail_link}: {response.status}")
                return
            thumbnail = await response.read()
            filename = f"{video_id}.jpg"
            uri = await self.client.upload_media(thumbnail, mime_type='image/jpeg', filename=filename)
            await self.client.send_image(evt.room_id, url=uri, file_name=filename, info=ImageInfo(mimetype='image/jpeg'))

    async def handle_instagram(self, evt, url_tup):
        L = instaloader.Instaloader(user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36")
        shortcode = url_tup[5]
        self.log.warning(shortcode)
        post = instaloader.Post.from_shortcode(L.context, shortcode)

        if self.config["instagram.info"]:
            await evt.reply(TextMessageEventContent(msgtype=MessageType.TEXT, format=Format.HTML, formatted_body=f"""<p>Username: {post.owner_username}<br>Caption: {post.caption}<br>Hashtags: {post.caption_hashtags}<br>Mentions: {post.caption_mentions}<br>Likes: {post.likes}<br>Comments: {post.comments}</p>"""))

        if (post.is_video and self.config["instagram.thumbnail"]) or (not post.is_video and self.config["instagram.image"]):
            response = await self.http.get(post.url)
            if response.status != 200:
                self.log.warning(f"Unexpected status fetching instagram image {post.url}: {response.status}")
                return

            media = await response.read()
            mime_type = 'image/jpeg'
            file_extension = ".jpg"
            file_name = shortcode + file_extension
            uri = await self.client.upload_media(media, mime_type=mime_type, filename=file_name)
            self.log.warning(f"{mime_type} {file_name}")
            await self.client.send_image(evt.room_id, url=uri, file_name=file_name, info=ImageInfo(mimetype='image/jpeg'))

        if post.is_video and self.config["instagram.video"]:
            response = await self.http.get(yarl.URL(post.video_url,encoded=True))
            if response.status != 200:
                self.log.warning(f"Unexpected status fetching instagram video {post.video_url}: {response.status}")
                return

            media = await response.read()
            mime_type = 'video/mp4'
            file_extension = ".mp4"
            file_name = shortcode + file_extension
            uri = await self.client.upload_media(media, mime_type=mime_type, filename=file_name)
            await self.client.send_file(evt.room_id, url=uri, info=BaseFileInfo(mimetype=mime_type, size=len(media)), file_name=file_name, file_type=MessageType.VIDEO)

    async def get_redirected_url(self, short_url: str) -> str:
        async with self.http.get(short_url, allow_redirects=True) as response:
            if response.status == 200:
                return str(response.url)
            else:
                self.log.warning(f"Unexpected status fetching redirected URL: {response.status}")
                return None
            
    async def send_image(self, evt, media_url, mime_type, file_name):
        response = await self.http.get(media_url)
        if response.status != 200:
            self.log.warning(f"Unexpected status fetching media {media_url}: {response.status}")
            return

        media = await response.read()
        uri = await self.client.upload_media(media, mime_type=mime_type, filename=file_name)
        await self.client.send_image(evt.room_id, url=uri, file_name=file_name, info=ImageInfo(mimetype=mime_type))

    async def handle_reddit(self, evt, url_tup):
        url = ''.join(url_tup).split('?')[0]

        if "/s/" in url:
            url = await self.get_redirected_url(url)
            if not url:
                return

        url = await self.get_redirected_url(url)
        query_url = quote(url).replace('%3A', ':') + ".json" + "?limit=1"
        headers = {'User-Agent': 'ggogel/SocialMediaDownloadMaubot'}
        response = await self.http.request('GET', query_url, headers=headers)

        if response.status != 200:
            self.log.warning(f"Unexpected status fetching reddit listing {query_url}: {response.status}")
            return

        response_text = await response.read()
        data = json.loads(response_text.decode())
        post_data = data[0]['data']['children'][0]['data']
        sub, title, name = post_data['subreddit_name_prefixed'], post_data['title'], post_data['name']

        if self.config["reddit.info"]:
            await evt.reply(TextMessageEventContent(msgtype=MessageType.TEXT, format=Format.HTML, body=f"{sub}: {title}", formatted_body=f"""<p><b>{sub}: {title}</b></p>"""))

        if 'url_overridden_by_dest' in post_data:
            media_url = post_data['url_overridden_by_dest']
            mime_type = mimetypes.guess_type(media_url)[0]

            if mime_type is None:
                if 'is_gallery' in post_data and post_data['is_gallery']:
                    for media_id, media_info in post_data['media_metadata'].items():
                        media_url = (media_info['s']['u']).replace("preview", "i")
                        mime_type = media_info['m']
                        file_name = media_id
                        await self.send_image(evt, media_url, mime_type, file_name)
                    return
                elif 'secure_media' in post_data and 'reddit_video' in post_data['secure_media']:
                    fallback_url = post_data['secure_media']['reddit_video']['fallback_url']
                elif 'preview' in post_data and 'reddit_video_preview' in post_data['preview']:
                    fallback_url = post_data['preview']['reddit_video_preview']['fallback_url']
                else:
                    self.log.warning(f"Unable to determine media url for {query_url}")
                    return
                
                media_url = fallback_url.split('?')[0]
                mime_type = mimetypes.guess_type(media_url)[0]

            file_extension = mimetypes.guess_extension(mime_type)
            file_name = name + file_extension

            if "image" in mime_type and self.config["reddit.image"]:
                await self.send_image(evt, media_url, mime_type, file_name)

            elif "video" in mime_type and self.config["reddit.video"]:
                audio_url = media_url.replace("DASH_720", "DASH_audio")
                url = urllib.parse.quote(url)
                download_url = f"https://sd.rapidsave.com/download.php?permalink={url}&video_url={media_url}?source=fallback&audio_url={audio_url}?source=fallback"
                response = await self.http.get(download_url)

                if response.status != 200:
                    self.log.warning(f"Unexpected status fetching media {download_url}: {response.status}")
                    return

                media = await response.read()

                if len(media) == 0:
                    self.log.warning(f"Received 0 bytes when fetching media {download_url}")
                    return

                uri = await self.client.upload_media(media, mime_type=mime_type, filename=file_name)
                await self.client.send_file(evt.room_id, url=uri, info=BaseFileInfo(mimetype=mime_type, size=len(media)), file_name=file_name, file_type=MessageType.VIDEO)

            elif self.config["reddit.image"] or self.config["reddit.video"]:
                self.log.warning(f"Unknown media type {query_url}: {mime_type}")
                return

    async def handle_bluesky(self, evt, url_tup):
        # Get user and post ID from the URL
        url = ''.join(url_tup)
        user, post_id = url.split("/")[-3], url.split("/")[-1]
        
        # Get the DID of the user
        did_url = f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={user}"
        async with self.http.get(did_url) as response:
            if response.status != 200:
                self.log.warning(f"Failed to resolve handle {user}: HTTP {response.status}")
                return
            did_data = await response.json()
            did = did_data.get("did")
            if not did:
                self.log.warning(f"No DID found for handle {user}")
                return
            
        self.log.info(f"Resolved Bluesky handle {user} to DID {did}")
            
        # Get the post using the DID and post ID and Bluesky's public relay API
        post_url = f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?uris=at://{did}/app.bsky.feed.post/{post_id}"
        async with self.http.get(post_url) as response:
            if response.status != 200:
                self.log.warning(f"Failed to fetch post {post_id}: HTTP {response.status}")
                return
            post_data = await response.json()
            post = post_data.get("posts", [])[0]
            if not post:
                self.log.warning(f"No post found for ID {post_id}")
                return
            
            content = post.get("record", {}).get("text", "")
            if content and self.config["bluesky.info"]:
                await evt.reply(TextMessageEventContent(msgtype=MessageType.TEXT, format=Format.HTML, body=content, formatted_body=content))

            # Handle attachments
            embed = post.get("embed", {})
            if embed:
                if "images" in embed:
                    # We'll use the fullsize image if available, otherwise the thumbnail
                    for image in embed["images"]:
                        fullsize = image.get("fullsize")
                        thumb = image.get("thumb")
                        alt = image.get("alt", "Bluesky Image")
                        if fullsize:
                            media_url = fullsize
                        elif thumb:
                            media_url = thumb
                        else:
                            # Are there other types of images that could be found?
                            continue
                        
                        mime_type = mimetypes.guess_type(media_url)[0] or "image/jpeg"
                        file_name = f"{post_id}_image.jpg"
                        await self.send_image(evt, media_url, mime_type, file_name)
                elif "playlist" in embed:
                    playlist_url = embed["playlist"]
                    thumbnail_url = embed.get("thumbnail")
                    self.log.info(f"Video URL: {playlist_url}, Thumbnail URL: {thumbnail_url}")
                    
                    if playlist_url and self.config["bluesky.video"]:
                        media_bytes = await self.download_m3u8_file(playlist_url)
                        if not media_bytes:
                            self.log.warning(f"Failed to download video from {playlist_url}")
                            return
                        
                        mime_type = "video/mp4"
                        file_name = f"{post_id}_video.mp4"
                        uri = await self.client.upload_media(media_bytes, mime_type=mime_type, filename=file_name)
                        await self.client.send_file(evt.room_id, url=uri, info=BaseFileInfo(mimetype=mime_type, size=len(media_bytes)), file_name=file_name, file_type=MessageType.VIDEO)
                    
                    if thumbnail_url and self.config["bluesky.thumbnail"]:
                        mime_type = mimetypes.guess_type(thumbnail_url)[0] or "image/jpeg"
                        file_name = f"{post_id}_thumbnail.jpg"
                        await self.send_image(evt, thumbnail_url, mime_type, file_name)
    
    async def download_m3u8_file(self, m3u8_url: str):
        async with self.http.get(m3u8_url) as response:
            if response.status != 200:
                self.log.warning(f"Failed to fetch playlist: {m3u8_url} — HTTP {response.status}")
                return b''
            playlist = await response.text()
        
        if "#EXT-X-STREAM-INF" in playlist:
            lines = playlist.strip().splitlines()
            next_m3u8 = next((line for line in lines if not line.startswith("#")), None)
            if not next_m3u8:
                self.log.warning(f"No variant found in master playlist: {m3u8_url}")
                return b''
            nested_url = urljoin(m3u8_url, next_m3u8)
            return await self.download_m3u8_file(nested_url)

        segment_urls = []
        base_url = m3u8_url.rsplit("/", 1)[0] + "/"
        for line in playlist.strip().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            segment_urls.append(urljoin(base_url, line.strip()))

        if not segment_urls:
            self.log.warning(f"No segments found in: {m3u8_url}")
            return b''

        segment_data = []
        for i, url in enumerate(segment_urls):
            async with self.http.get(url) as segment_response:
                if segment_response.status != 200:
                    self.log.warning(f"Failed to download segment {i + 1}: {url} — HTTP {segment_response.status}")
                    continue
                segment_data.append(await segment_response.read())
                if i % 10 == 0:
                    self.log.debug(f"Downloaded segment {i + 1}/{len(segment_urls)}")

        if not segment_data:
            self.log.warning("All segment downloads failed.")
            return b''

        self.log.info("All segments downloaded.")
        return b"".join(segment_data)
