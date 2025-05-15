import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
import json
from datetime import datetime, timedelta
import time
import requests
# musicbrainzngs import removed

# --- Configuration ---
CLIENT_ID = 'cba39c5427f740aa8e4e6961ac5762cf'
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET') # Loaded from environment variable
REDIRECT_URI = 'https://127.0.0.1:8888/callback' # This MUST exactly match the URI in your Spotify Developer Dashboard
# It's highly recommended to set these as actual environment variables
# or use a more secure method for managing credentials for production use.

PLAYLIST_NAME = "180 BPM Running Hits"
MIN_BPM = 175
MAX_BPM = 185
LIKED_SONGS_CACHE_FILE = "liked_songs_cache.json"
# USER_AGENT_APP_NAME = "SpotifyBPMPlaylistCreator" # No longer needed for MusicBrainz
# USER_AGENT_APP_VERSION = "0.1" # No longer needed for MusicBrainz
# USER_AGENT_CONTACT_INFO = "demianglait@gmail.com" # No longer needed for MusicBrainz

GETSONGBPM_API_KEY = "YOUR_GETSONGBPM_API_KEY"  # <<< IMPORTANT: Replace with your actual API key
GETSONGBPM_API_URL = "https://api.getsongbpm.com/search/"
BPM_DATA_CACHE_DIR = "bpm_data_cache" # Cache for BPM data from GetSongBPM

# Scope required for accessing liked songs and modifying playlists
SCOPE = "user-library-read playlist-modify-public playlist-modify-private"

# MusicBrainz client configuration is no longer needed
# musicbrainzngs.set_useragent(...)


def get_spotify_client():
    """Authenticates with Spotify API and returns a client object."""
    # Using direct credential passing and explicitly managing browser opening
    # if the local server for callback (especially with HTTPS) is problematic.
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        open_browser=False  # This will print an auth URL to the console.
                            # You'll need to open it, authorize, then paste the
                            # resulting URL (from your browser's address bar)
                            # back into the terminal when prompted.
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    return sp


def get_liked_songs(sp):
    """
    Fetches all liked songs for the current user.
    Uses a local cache to avoid fetching if data is less than 24 hours old.
    """
    if os.path.exists(LIKED_SONGS_CACHE_FILE):
        try:
            with open(LIKED_SONGS_CACHE_FILE, 'r') as f:
                cache_data = json.load(f)
            last_fetched_str = cache_data.get('timestamp')
            cached_songs = cache_data.get('songs')

            if last_fetched_str and cached_songs:
                last_fetched_dt = datetime.fromisoformat(last_fetched_str)
                if datetime.now() - last_fetched_dt < timedelta(days=1):
                    print(f"Loading {len(cached_songs)} liked songs from local cache (less than 24 hours old).")
                    return cached_songs
                else:
                    print("Cache is older than 24 hours. Fetching fresh data.")
            else:
                print("Cache data incomplete. Fetching fresh data.")
        except (json.JSONDecodeError, IOError, TypeError, ValueError) as e:
            print(f"Cache file corrupted, unreadable, or invalid format, fetching fresh data: {e}")

    print("Fetching liked songs from Spotify...")
    liked_songs = []
    offset = 0
    limit = 50  # Max limit per request
    while True:
        try:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            if not results or not results['items']:
                break
            for item in results['items']:
                track = item.get('track')
                if track and track.get('id') and track.get('name') and track.get('artists'):
                    isrc = track.get('external_ids', {}).get('isrc')
                    liked_songs.append({
                        'id': track['id'],
                        'name': track['name'],
                        'artist': track['artists'][0]['name'] if track['artists'] else 'Unknown Artist',
                        'isrc': isrc
                    })
            offset += len(results['items']) # More robust way to increment offset
            if not results['next']: # Check if there's a next page
                break
            print(f"Fetched {len(liked_songs)} liked songs so far...")
        except Exception as e:
            print(f"Error fetching liked songs batch: {e}")
            # Decide if you want to break or retry, for now, we break
            break
            
    print(f"Total liked songs fetched from Spotify: {len(liked_songs)}")

    # Save to cache
    cache_data_to_save = {'timestamp': datetime.now().isoformat(), 'songs': liked_songs}
    try:
        with open(LIKED_SONGS_CACHE_FILE, 'w') as f:
            json.dump(cache_data_to_save, f, indent=4)
        print(f"Saved {len(liked_songs)} liked songs to cache at {LIKED_SONGS_CACHE_FILE}")
    except IOError as e:
        print(f"Error saving liked songs to cache: {e}")
        
    return liked_songs


def fetch_bpm_from_getsongbpm_api(song_info, api_key):
    """
    Fetches BPM from GetSongBPM API using song title and artist.
    Caches the result locally based on Spotify track ID.
    """
    spotify_id = song_info.get('id')
    song_name = song_info.get('name')
    artist_name = song_info.get('artist')

    if not spotify_id:
        print("  Error: Spotify ID missing from song_info. Cannot use GetSongBPM cache.")
        # This case should ideally not be reached if liked_songs are fetched correctly.
        # If it occurs, we might skip caching or the API call for this song.
        return None 

    # Ensure BPM data cache directory exists
    if not os.path.exists(BPM_DATA_CACHE_DIR):
        try:
            os.makedirs(BPM_DATA_CACHE_DIR)
            print(f"Created BPM data cache directory: {BPM_DATA_CACHE_DIR}")
        except OSError as e:
            print(f"Error creating BPM data cache directory {BPM_DATA_CACHE_DIR}: {e}")
            # Proceed without caching if directory creation fails
            pass
    
    cache_file_path = os.path.join(BPM_DATA_CACHE_DIR, f"{spotify_id}.json")

    # Check cache first
    if os.path.exists(cache_file_path):
        try:
            with open(cache_file_path, 'r') as f:
                cached_data = json.load(f)
                bpm = cached_data.get('bpm')
                if 'bpm' in cached_data: # Check if 'bpm' key exists, even if value is None
                    print(f"  Loaded BPM ({bpm}) from local cache for Spotify ID {spotify_id} ('{song_name}').")
                    return bpm
        except (json.JSONDecodeError, IOError, TypeError) as e:
            print(f"  Error reading BPM cache for Spotify ID {spotify_id} ('{song_name}'): {e}. Fetching from API.")

    # If not cached or cache invalid, fetch from API
    print(f"  Fetching BPM from GetSongBPM API for '{song_name}' by '{artist_name}'...")
    
    bpm_to_cache = None # Default to None if not found or error
    
    if not api_key or api_key ***REMOVED*** "YOUR_GETSONGBPM_API_KEY":
        print("  Error: GetSongBPM API key is not configured. Skipping API call.")
        # Save None to cache to avoid repeated attempts without a key
        if os.path.exists(BPM_DATA_CACHE_DIR):
             try:
                with open(cache_file_path, 'w') as f:
                    json.dump({'bpm': None}, f)
             except IOError as e:
                print(f"  Error saving unconfigured API status to cache for '{song_name}': {e}")
        return None

    params = {
        'api_key': api_key,
        'type': 'song',
        'lookup': f"song:{song_name} artist:{artist_name}"
    }
    
    try:
        response = requests.get(GETSONGBPM_API_URL, params=params, timeout=15) # Increased timeout
        response.raise_for_status()  # Raises an exception for HTTP errors (4XX, 5XX)
        data = response.json()
        
        # GetSongBPM returns a list under 'search_results'. We'll take the first good match.
        # A direct match often has 'song_title' and 'artist_name' fields.
        # The BPM is in 'tempo'.
        if data.get('search_results') and isinstance(data['search_results'], list) and len(data['search_results']) > 0:
            # Iterate through results to find a close match if needed, but often the first is best.
            # For simplicity, we'll check the first result.
            # A more robust match might compare titles/artists more closely.
            first_result = data['search_results'][0]
            api_bpm_str = first_result.get('tempo')
            if api_bpm_str:
                bpm_to_cache = float(api_bpm_str)
                print(f"  Found BPM {bpm_to_cache} on GetSongBPM for '{song_name}' by '{artist_name}'.")
            else:
                print(f"  BPM not found in GetSongBPM API response for '{song_name}'. Response: {first_result}")
        else:
            print(f"  No search results or unexpected format from GetSongBPM for '{song_name}'. Full response: {data}")
            
    except requests.exceptions.HTTPError as e:
        if e.response.status_code ***REMOVED*** 404:
            print(f"  Song '{song_name}' by '{artist_name}' not found on GetSongBPM (404).")
        elif e.response.status_code ***REMOVED*** 401 or e.response.status_code ***REMOVED*** 403:
            print(f"  GetSongBPM API request unauthorized/forbidden (401/403) for '{song_name}'. Check API key. Error: {e}")
        else:
            print(f"  GetSongBPM API HTTP error for '{song_name}': {e}")
    except requests.exceptions.RequestException as e:
        print(f"  GetSongBPM API request error for '{song_name}': {e}")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  Error parsing GetSongBPM response or BPM for '{song_name}': {e}")
    except Exception as e:
        print(f"  Unexpected error fetching/processing GetSongBPM data for '{song_name}': {e}")

    # Save to cache, even if BPM was not found (to avoid re-fetching)
    if os.path.exists(BPM_DATA_CACHE_DIR): # Only try to save if dir exists
        try:
            with open(cache_file_path, 'w') as f:
                json.dump({'bpm': bpm_to_cache}, f)
        except IOError as e:
            print(f"  Error saving BPM to local cache for '{song_name}': {e}")
            
    return bpm_to_cache


def filter_songs_by_bpm(songs, min_bpm, max_bpm):
    """
    Filters songs based on their BPM, fetched from GetSongBPM API.
    """
    print(f"Filtering songs by BPM ({min_bpm}-{max_bpm}) using GetSongBPM API...")
    filtered_spotify_song_ids = []
    
    if not GETSONGBPM_API_KEY or GETSONGBPM_API_KEY ***REMOVED*** "YOUR_GETSONGBPM_API_KEY":
        print("Error: GetSongBPM API key is not configured in the script.")
        print("Please set GETSONGBPM_API_KEY at the top of the script.")
        print("Skipping BPM filtering.")
        return [] # Return empty list as we can't filter

    total_songs = len(songs)
    for index, song_info in enumerate(songs):
        spotify_id = song_info['id']
        song_name = song_info['name']
        artist_name = song_info['artist']
        
        print(f"\nProcessing song {index + 1}/{total_songs}: '{song_name}' by '{artist_name}' (Spotify ID: {spotify_id})")

        bpm = fetch_bpm_from_getsongbpm_api(song_info, GETSONGBPM_API_KEY)
        
        if bpm and min_bpm <= bpm <= max_bpm:
            print(f"  >>> ADDING '{song_name}' by {artist_name} (BPM: {bpm:.2f}) to playlist.")
            filtered_spotify_song_ids.append(spotify_id)
        elif bpm:
            print(f"  Skipping '{song_name}' by {artist_name} (BPM: {bpm:.2f} - outside range).")
        else:
            # Message already printed by fetch_bpm_from_getsongbpm_api if BPM not found or error
            print(f"  Skipping '{song_name}' by {artist_name}' (BPM not found or error from GetSongBPM).")

        # Respect API rate limits - GetSongBPM free tier is ~1 req/sec (60/min)
        # Check their current limits if you encounter issues.
        time.sleep(1.1) # Slightly more than 1 second to be safe

    print(f"\nFound {len(filtered_spotify_song_ids)} songs within the desired BPM range using GetSongBPM API.")
    return filtered_spotify_song_ids


def create_playlist(sp, user_id, playlist_name):
    """Creates a new playlist."""
    print(f"Creating playlist: '{playlist_name}'...")
    playlist = sp.user_playlist_create(user=user_id, name=playlist_name, public=False) # Set public=True if you want
    print(f"Playlist '{playlist['name']}' created with ID: {playlist['id']}")
    return playlist['id']


def add_songs_to_playlist(sp, playlist_id, song_ids):
    """Adds songs to the specified playlist."""
    if not song_ids:
        print("No songs to add to the playlist.")
        return

    print(f"Adding {len(song_ids)} songs to the playlist...")
    # Spotify API allows adding up to 100 tracks at a time
    for i in range(0, len(song_ids), 100):
        batch_ids = song_ids[i:i + 100]
        sp.playlist_add_items(playlist_id, batch_ids)
    print("Songs added successfully.")


def main():
    """Main function to orchestrate the playlist creation."""
    # Credentials are now defined as constants at the top of the script (CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
    # and passed directly to SpotifyOAuth.
    # For better security, consider moving CLIENT_ID and CLIENT_SECRET to actual environment variables
    # and loading them using os.getenv() here.
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        print("Error: Spotify API credentials (CLIENT_ID, CLIENT_SECRET, REDIRECT_URI) are not configured in the script.")
        print("Please define them near the top of the spotify_playlist_creator.py file.")
        return

    print(f"Attempting to authenticate with Spotify...")
    print(f"Using Redirect URI: {REDIRECT_URI}")
    print("If prompted, please open the authorization URL in your browser, authorize the application,")
    print("and then paste the FULL URL from your browser's address bar back into the terminal.")
    
    sp = get_spotify_client()
    
    try:
        user_info = sp.current_user()
        if not user_info or not user_info['id']:
            print("Error: Could not retrieve user information. Authentication may have failed or the token is invalid.")
            return
        user_id = user_info['id']
        print(f"Successfully authenticated as user: {user_info.get('display_name', user_id)}")
    except Exception as e:
        print(f"Error during initial authentication or fetching user information: {e}")
        print("Please double-check the following:")
        print("1. Your CLIENT_ID, CLIENT_SECRET, and REDIRECT_URI in the script are correct.")
        print(f"2. The REDIRECT_URI ('{REDIRECT_URI}') EXACTLY matches one registered in your Spotify Developer Dashboard for this app.")
        print("3. If pasting a URL, ensure it's the complete URL from the browser after authorization (even if the page shows an error).")
        return

    liked_songs = get_liked_songs(sp)
    if not liked_songs:
        print("No liked songs found or couldn't fetch them.")
        return

    songs_in_bpm_range_ids = filter_songs_by_bpm(liked_songs, MIN_BPM, MAX_BPM) # Removed sp
    if not songs_in_bpm_range_ids:
        print(f"No songs found between {MIN_BPM} and {MAX_BPM} BPM.")
        return

    playlist_id = create_playlist(sp, user_id, PLAYLIST_NAME)
    add_songs_to_playlist(sp, playlist_id, songs_in_bpm_range_ids)

    print(f"\nPlaylist '{PLAYLIST_NAME}' has been created/updated with songs between {MIN_BPM}-{MAX_BPM} BPM.")
    print(f"You can find it in your Spotify account.")


if __name__ ***REMOVED*** "__main__":
    main()
