import datetime
import io
import json
import os
import sqlite3
from multiprocessing.pool import ThreadPool
from time import time
import traceback

import libxmp
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sanitize_filename import sanitize
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from tqdm import tqdm

"""
Archiver for Google Photos
By: Nick Dawson | nick@ndawson.me
"""

"""
    Archiver For Google Photos
    - A tool to maintain an archive/mirror of your Google Photos library for backup purposes.
    Copyright (C) 2021  Nicholas Dawson

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""

VERSION = "2.2.0"

# Define Scopes for Application
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.readonly",  # Read Only Photos Library API
]

# Define constants
DATABASE_NAME = "database.sqlite3"


def safe_mkdir(path):
    """
    Creates directory only if it doesn't exist already to prevent errors
    """
    if not os.path.exists(path):
        os.mkdir(path)


def auto_mkdir(path, instance=0):
    """
    Recursively creates directory and appends a number -> (#) to the end
    if that directory already exists
    """
    if instance:
        new_path = path + " (" + str(instance) + ")"
    else:
        new_path = path

    if not os.path.exists(new_path):
        os.mkdir(new_path)
        return os.path.abspath(new_path)
    else:
        return auto_mkdir(path, instance + 1)


def auto_filename(path, instance=0):
    """
    Recursively finds an available name for a new file and
    appends a number -> (#) to the end if that file already exists
    """
    if instance:
        extension_index = path.rfind(".")
        new_path = (
            path[:extension_index] + " (" + str(instance) + ")" + path[extension_index:]
        )
    else:
        new_path = path

    if not os.path.exists(new_path):
        return new_path
    else:
        return auto_filename(path, instance + 1)


def save_json(variable, path):
    json.dump(variable, open(path, "w"))


def load_json(path):
    # If file exists load the json as a dict
    if os.path.isfile(path):
        return json.load(open(path, "r"))
    # If file doesn't exist return None
    else:
        return None


def write_response(r, path):
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1000000):
            f.write(chunk)


class PhotosAccount(object):
    def __init__(self, credentials_path, directory, thread_count, debug):
        # Define directory instance variables
        self.base_dir = directory
        self.lib_dir = self.base_dir + "/Library"
        self.albums_dir = self.base_dir + "/Albums"
        self.shared_albums_dir = self.base_dir + "/Shared Albums"
        self.favorites_dir = self.base_dir + "/Favorites"

        # Define/initialize other instance variables
        self.thread_count = thread_count
        self.credentials = credentials_path
        self.service = None  # is None because it will be defined later by calling "get_google_api_service"
        self.timer = time()
        self.downloads = 0
        self.debug = debug

        if self.debug:
            self.debug_dir = self.base_dir + "/debug"
            safe_mkdir(self.debug_dir)

        # Define/Init Database
        self.db_path = self.base_dir + "/" + DATABASE_NAME
        self.con = self.init_db()
        self.cur = self.con.cursor()

        # Create the directories (if not already there)
        safe_mkdir(self.base_dir)
        safe_mkdir(self.lib_dir)
        safe_mkdir(self.albums_dir)
        safe_mkdir(self.shared_albums_dir)
        safe_mkdir(self.favorites_dir)

    def get_google_api_service(self):
        # The file photos_token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first time.
        credentials = None
        token_path = self.base_dir + "/photoslibrary_token.json"

        try:
            credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            pass

        # If there are no (valid) credentials available, let the user log in.
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                if not os.path.exists(self.credentials):
                    raise FileNotFoundError(self.credentials + " is not found.")

                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials,
                    scopes=SCOPES,
                    redirect_uri='urn:ietf:wg:oauth:2.0:oob',
                )
                auth_url, _ = flow.authorization_url(prompt='consent')
                print('Please go to this URL: {}'.format(auth_url))
                code = input('Enter the authorization code: ')
                flow.fetch_token(code=code)
                credentials = flow.credentials
            # Save the credentials for the next run
            with open(token_path, "w", encoding="utf-8") as token:
                token.write(credentials.to_json())

        self.service = build(
            "photoslibrary", "v1", credentials=credentials, static_discovery=False
        )

    def init_db(self):
        if not os.path.exists(self.db_path):
            con = sqlite3.connect(self.db_path)
            cur = con.cursor()
            # Create Media Table - Used to store basic information about each media item
            cur.execute(
                """CREATE TABLE media (uuid text, path text, album_uuid text)"""
            )
            # Create Albums Table - Used to store information about each album
            cur.execute(
                """CREATE TABLE albums (uuid text, path text, title text, is_shared integer)"""
            )
            con.commit()  # Save changes
            return con
        else:
            return sqlite3.connect(self.db_path)

    def get_session_stats(self):
        return time() - self.timer, self.downloads

    def download_media_item(self, entry):
        try:
            uuid, album_uuid, url, path, description, creation_time = entry
            if not os.path.isfile(path):
                with requests.get(url, stream=True) as r:
                    if r.status_code == 200:
                        path = auto_filename(path)
                        write_response(r, path)
                        if description or creation_time is not None:
                            xmpfile = None
                            xmp = None
                            try:
                                try:
                                    xmpfile = libxmp.XMPFiles(file_path=path, open_forupdate=True)
                                    xmp = xmpfile.get_xmp()
                                except Exception:
                                    pass
                                if xmp is None:
                                    print(f" [INFO] {path}: can't parse EXIF data, creating from scratch")
                                    xmp = libxmp.XMPMeta()

                                if description:
                                    xmp.set_property(libxmp.consts.XMP_NS_EXIF, "UserComment", description)

                                if creation_time is not None:
                                    creation_time_dt = datetime.datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
                                    xmp.set_property(libxmp.consts.XMP_NS_EXIF, "DateTimeOriginal", creation_time_dt.isoformat())
                                    xmp.set_property(libxmp.consts.XMP_NS_XMP, "CreateDate", creation_time_dt.isoformat())

                                saved = False
                                try:
                                    if xmpfile is not None and xmpfile.can_put_xmp(xmp):
                                        xmpfile.put_xmp(xmp)
                                        xmpfile.close_file(close_flags=libxmp.consts.XMP_CLOSE_SAFEUPDATE)
                                        xmpfile = None
                                        saved = True
                                except Exception as e:
                                    print(f" [INFO] {path}: error updating embedded EXIF data: {e}")

                                if not saved:
                                    with open(path + ".xmp", "w", encoding="utf-8") as f:
                                        f.write(xmp.serialize_to_str())
                            except Exception as e:
                                print(f" [INFO] {path}: error updating EXIF data: {e}")
                            finally:
                                if xmpfile is not None:
                                    xmpfile.close_file()

                        self.downloads += 1
                        return (
                            uuid,
                            path,
                            album_uuid,
                        )

            else:
                return False
        except Exception as e:
            print(" [ERROR] media item could not be downloaded because:", e)
            traceback.print_exc()
            return False

    def download(self, entries, desc, thread_count):
        result = ThreadPool(thread_count).imap_unordered(
            self.download_media_item, entries
        )
        for downloaded_entry in tqdm(
            result, unit=" media items", total=len(entries), desc=desc
        ):
            if downloaded_entry:
                uuid, path, album_uuid = downloaded_entry
                self.insert_media_item(
                    uuid,
                    path,
                    album_uuid,
                )

    def select_media_item(self, uuid):
        return self.cur.execute(
            """SELECT * FROM media WHERE uuid=?""", (uuid,)
        ).fetchone()

    def insert_media_item(self, uuid, path, album_uuid):
        self.cur.execute(
            """INSERT INTO media VALUES (?, ?, ?)""", (uuid, path, album_uuid)
        )
        self.con.commit()

    def select_album(self, uuid):
        return self.cur.execute(
            """SELECT * FROM albums WHERE uuid=?""", (uuid,)
        ).fetchone()

    def insert_album(self, uuid, path, title, is_shared=False):
        self.cur.execute(
            """INSERT INTO albums VALUES (?, ?, ?, ?)""", (uuid, path, title, is_shared)
        )
        self.con.commit()

    def process_media_items(self, media_items, save_directory, album_uuid=None):
        media = []
        for item in media_items:
            # Path where the media item will be saved to
            item_path = None

            # Select the media item from the database
            # -> if it doesn't exist then generate the item_path
            # -> if it already exists then just pull the item_path from the existing db entry
            item_db_entry = self.select_media_item(item["id"])
            if not item_db_entry:
                item["filename"] = sanitize(item["filename"])
                item_path = f'{save_directory}/{item["filename"]}'
            else:
                item_path = item_db_entry[1]

            # Set description to none if not there so a key error won't occur below
            #   This keeps the code simpler when dealing with descriptions
            if "description" not in item:
                item["description"] = None

            # Process Media
            # - Image
            if "image" in item["mimeType"]:
                media.append(
                    (
                        item["id"],
                        album_uuid,
                        item["baseUrl"] + "=d",
                        item_path,
                        item["description"],
                        item["mediaMetadata"]["creationTime"],
                    )
                )
            # - Video
            elif "video" in item["mimeType"]:
                media.append(
                    (
                        item["id"],
                        album_uuid,
                        item["baseUrl"] + "=dv",
                        item_path,
                        item["description"],
                        item["mediaMetadata"]["creationTime"],
                    )
                )

        return media

    def download_library(self):
        items = self.process_media_items(self.list_media_items(), self.lib_dir)
        self.download(items, "Downloading Library", self.thread_count)

    def download_favorites(self):
        items = self.process_media_items(self.search_favorites(), self.favorites_dir)
        self.download(items, "Downloading Favorites", self.thread_count)

    def download_all_albums(self):
        for album in self.list_albums():
            self.download_single_album(album)

    def download_all_shared_albums(self):
        for album in self.list_shared_albums():
            self.download_single_album(album, True)

    def download_single_album(self, album, shared=False):
        # Return if the album has no mediaItems to download
        # Unsure of how this occurs, but there are album entries that exist
        #   where there I don't have permission, weird bug...
        if "mediaItemsCount" not in album:
            return

        # Next check to see if the album has a title, if it doesn't give it default name
        if "title" not in album:
            album["title"] = "Unnamed Album"

        # Sanitize album title
        album["title"] = sanitize(album["title"])

        # Make request
        album_items = []

        request_body = {
            "albumId": album["id"],
            "pageSize": 100,  # Max is 100
            "pageToken": "",
        }
        num = 0
        request = (
            self.service.mediaItems().search(body=request_body).execute()
        )  # 100 is max
        if not request:
            return
        while True:
            if "mediaItems" in request:
                album_items += request["mediaItems"]
            if "nextPageToken" in request:
                request_body["pageToken"] = request["nextPageToken"]
                request = self.service.mediaItems().search(body=request_body).execute()
            else:
                break

        if self.debug:
            save_json(album_items, self.debug_dir + "/album-" + album["id"] + ".json")

        # Directory where the album exists
        album_path = None

        # Select the album item from the database
        # -> if it exists then insert a new album entry and set the album_path to the newly set path
        # -> if it already exists then just pull the album_path from the existing db entry
        album_db_entry = self.select_album(album["id"])
        if album_db_entry:
            album_path = album_db_entry[1]
        elif not shared:
            album_path = auto_mkdir(self.albums_dir + "/" + album["title"])
            self.insert_album(album["id"], album_path, album["title"], shared)
        else:
            album_path = auto_mkdir(self.shared_albums_dir + "/" + album["title"])
            self.insert_album(album["id"], album_path, album["title"], shared)

        processed_items = self.process_media_items(album_items, album_path, album["id"])

        if processed_items:
            self.download(
                processed_items,
                f"Downloading {'Shared ' if shared else ''}Album: \"{album['title']}\"",
                self.thread_count,
            )
        else:
            print(
                f"Downloading {'Shared ' if shared else ''}Album: \"{album['title']}\""
            )
            print("Everything already downloaded.")

    def list_media_items(self):
        num = 0
        media_items_list = []
        request = self.service.mediaItems().list(pageSize=100).execute()  # Max is 50
        if not request:
            return {}
        while True:
            if self.debug:
                save_json(request, self.debug_dir + "/media" + str(num) + ".json")
            if "mediaItems" in request:
                media_items_list += request["mediaItems"]
            if "nextPageToken" in request:
                next_page = request["nextPageToken"]
                request = (
                    self.service.mediaItems()
                    .list(pageSize=100, pageToken=next_page)
                    .execute()
                )
            else:
                break
            num += 1
        if self.debug:
            save_json(media_items_list, self.debug_dir + "/media_items_list.json")
        return media_items_list

    def list_albums(self):
        num = 0
        album_list = []
        request = self.service.albums().list(pageSize=50).execute()  # Max is 50
        if not request:
            return {}
        while True:
            if self.debug:
                save_json(request, self.debug_dir + "/albums" + str(num) + ".json")
            if "albums" in request:
                album_list += request["albums"]
            if "nextPageToken" in request:
                next_page = request["nextPageToken"]
                request = (
                    self.service.albums()
                    .list(pageSize=50, pageToken=next_page)
                    .execute()
                )
            else:
                break
            num += 1
        if self.debug:
            save_json(album_list, self.debug_dir + "/album_list.json")
        return album_list

    def list_shared_albums(self):
        shared_album_list = []
        request = self.service.sharedAlbums().list(pageSize=50).execute()  # Max is 50
        num = 0
        if not request:
            return {}
        while True:
            if self.debug:
                save_json(request, self.debug_dir + "/shared_albums" + str(num) + ".json")
            shared_album_list += request["sharedAlbums"]
            if "nextPageToken" in request:
                next_page = request["nextPageToken"]
                request = (
                    self.service.sharedAlbums()
                    .list(pageSize=50, pageToken=next_page)
                    .execute()
                )
            else:
                break
            num += 1
        if self.debug:
            save_json(shared_album_list, self.debug_dir + "/shared_album_list.json")
        return shared_album_list

    def search_favorites(self):
        # Form request body using media_types_list above
        request_body = {
            "filters": {"featureFilter": {"includedFeatures": ["FAVORITES"]}},
            "pageSize": 100,  # Max is 100
            "pageToken": "",
        }
        num = 0
        # Make request
        favorites_list = []
        request = self.service.mediaItems().search(body=request_body).execute()
        if not request:
            return {}
        while True:
            if self.debug:
                save_json(request, self.debug_dir + "/favorites" + str(num) + ".json")
            if "mediaItems" in request:
                favorites_list += request["mediaItems"]
            if "nextPageToken" in request:
                request_body["pageToken"] = request["nextPageToken"]
                request = self.service.mediaItems().search(body=request_body).execute()
            else:
                break
            num += 1
        if self.debug:
            save_json(favorites_list, self.debug_dir + "/favorites_list.json")
        return favorites_list
