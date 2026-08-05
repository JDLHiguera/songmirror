"""Tidal target — using tidalapi."""

import json
import os
import time

from ..logs import log_warn
from ..matching import normalize_text, romanized, score_candidate
from .base import MirrorTarget, TargetAuthError


def build(opts=None):
    from ..logs import log_note
    try:
        auth_file = os.getenv("TIDAL_AUTH_FILE") or "data/tidal_oauth.json"
        return TidalTarget(auth_file)
    except TargetAuthError as e:
        log_note(f"Tidal skipped: {e}", tag="tidal")
        return None


class TidalTarget(MirrorTarget):
    name = "Tidal"
    tag = "tidal"
    source = "tidal"

    def __init__(self, auth_file):
        import tidalapi
        
        self.auth_file = auth_file
        self.cache_file = os.getenv("TIDAL_CACHE_FILE") or "data/tidal_resolve_cache.json"
        
        if not auth_file or not os.path.exists(auth_file):
            raise TargetAuthError("Tidal not configured (no auth file)")
            
        with open(auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        self.session = tidalapi.Session()
        try:
            self.session.load_oauth_session(
                token_type=data.get("token_type", "Bearer"),
                access_token=data.get("access_token", ""),
                refresh_token=data.get("refresh_token", "")
            )
            # Fetch user to verify authentication is still valid
            if not self.session.user:
                raise TargetAuthError("Tidal user not found")
        except Exception as e:
            raise TargetAuthError(f"Tidal authentication failed: {e}")

    def list_playlists(self):
        out = {}
        try:
            for pl in self.session.user.playlists():
                key = (pl.name or "").strip().casefold()
                if key and key not in out:
                    out[key] = {"id": str(pl.id), "name": pl.name or "", "_obj": pl, "_owned": True}
        except Exception as e:
            log_warn(f"Failed to read created Tidal playlists: {e}", tag=self.tag)
            
        try:
            for pl in self.session.user.favorites.playlists():
                key = (pl.name or "").strip().casefold()
                if key and key not in out:
                    out[key] = {"id": str(pl.id), "name": pl.name or "", "_obj": pl, "_owned": False}
        except Exception as e:
            log_warn(f"Failed to read favorite Tidal playlists: {e}", tag=self.tag)
            
        return out

    def is_editable(self, playlist):
        # In tidalapi, UserPlaylist has an edit method, others don't.
        # Check if the playlist creator is the logged-in user.
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        return str(getattr(getattr(pl, "creator", None), "id", "")) == str(self.session.user.id)

    def create(self, sp_playlist):
        from .. import spotify
        name = sp_playlist.get("name", "")
        desc = spotify.description(sp_playlist) or ""
        pl = self.session.user.create_playlist(name, desc)
        return {"id": str(pl.id), "name": pl.name or "", "_obj": pl, "_owned": True}

    def playlist_tracks(self, playlist):
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        out = []
        try:
            tracks = pl.tracks(limit=10000)
        except Exception as e:
            log_warn(f"failed to read Tidal playlist tracks: {e}", tag=self.tag)
            return out

        for t in tracks:
            # t can be a Track or Video
            if not hasattr(t, "id"):
                continue
            artists = []
            if hasattr(t, "artists") and getattr(t, "artists", None):
                artists = [a.name for a in t.artists if hasattr(a, "name")]
            out.append({
                "relationship_id": str(t.id),
                "name": t.name or "",
                "artist": t.artist.name if hasattr(t, "artist") and getattr(t, "artist", None) else "",
                "artists": artists,
                "duration_ms": int((t.duration or 0) * 1000),
                "isrc": t.isrc if hasattr(t, "isrc") else None,
            })
        return out

    def track_id(self, track):
        return track.get("relationship_id")

    def playlist_count(self, playlist):
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        return getattr(pl, "num_tracks", None)

    def playlist_name(self, playlist):
        if isinstance(playlist, dict):
            return playlist.get("name", "")
        return playlist.name or ""

    def playlist_description(self, playlist):
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        return getattr(pl, "description", "")

    def expected_ids(self, sp_tracks, links, cache):
        out = {}
        for t in sp_tracks:
            ids = set()
            if links.get(t.get("id")):
                ids.add(links[t["id"]])
            if ids:
                out[t.get("id")] = ids
        return out

    def resolve(self, track, cache):
        return self._search(track["name"], track["artists"], track["duration_ms"], cache, track.get("artist", "")), "search"

    def _search_once(self, term, name, artists, duration_ms):
        import tidalapi
        
        try:
            results = self.session.search(term, models=[tidalapi.media.Track], limit=10)
        except Exception:
            return None
            
        tracks = results.get("tracks", [])
        if not tracks:
            return None

        best_id, best_score = None, -1.0
        for t in tracks:
            t_artists = [a.name for a in t.artists] if getattr(t, "artists", None) else []
            t_artist = t.artist.name if getattr(t, "artist", None) else ""
            
            score, ok = score_candidate(
                name, artists, duration_ms,
                t.name or "", t_artist, int((t.duration or 0) * 1000)
            )
            if ok and score > best_score:
                best_id, best_score = str(t.id), score
                
        return best_id

    def _search(self, name, artists, duration_ms, cache, track_artist=""):
        from ..matching import track_key
        primary = artists[0] if artists else ""
        if not f"{name} {primary}".strip():
            return None
            
        key = track_key(name, track_artist)
        if key in cache.get("search", {}):
            val = cache["search"][key]
            if isinstance(val, str) and "tidal.com" in val and "/track/" in val:
                val = val.split("/track/")[1].split("/")[0].split("?")[0]
            return val
            
        if "search" not in cache:
            cache["search"] = {}

        best = self._search_once(f"{name} {primary}".strip(), name, artists, duration_ms)
        if not best:
            rom = f"{romanized(name)} {romanized(primary)}".strip()
            if rom and rom != normalize_text(f"{name} {primary}"):
                time.sleep(0.3)
                best = self._search_once(rom, name, artists, duration_ms)
                
        cache["search"][key] = best
        cache["dirty"] = True
        time.sleep(0.3)
        return best

    def add(self, playlist, target_ids):
        # tidalapi playlist.add has a strict limit on the number of items per request
        # (usually 50-100), otherwise it returns HTTP 400.
        if not target_ids:
            return
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        
        # Chunk into batches of 50
        batch_size = 50
        for i in range(0, len(target_ids), batch_size):
            chunk = target_ids[i:i + batch_size]
            try:
                pl.add(chunk)
                from ..config import polite_sleep
                polite_sleep(0.5)
            except Exception as e:
                log_warn(f"Failed to add tracks to Tidal playlist (batch {i}): {e}", tag=self.tag)

    def remove(self, playlist, track):
        pl = playlist.get("_obj") if isinstance(playlist, dict) else playlist
        try:
            pl.remove_by_id(track["relationship_id"])
        except Exception as e:
            log_warn(f"Failed to remove track from Tidal playlist: {e}", tag=self.tag)
