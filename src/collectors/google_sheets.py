"""Google Drive & Google Sheets OAuth2 client for inspecting and updating spreadsheets."""

import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import httpx
import structlog

from src.config import settings
from src.collectors.base import ScraperError

logger = structlog.get_logger(__name__)


def extract_spreadsheet_id_from_url(raw_input: str) -> Optional[str]:
    """Extract spreadsheet ID if raw_input is a docs.google.com/spreadsheets/d/<ID> URL."""
    text = (raw_input or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{15,})", text)
    if m:
        return m.group(1)
    return None


def normalize_values_payload(raw_values: Any) -> List[List[Any]]:
    """Normalize a JSON string, 1D list, 2D list, or scalar into a 2D list of rows for Google Sheets."""
    parsed = raw_values
    if isinstance(raw_values, str):
        stripped = raw_values.strip()
        if not stripped:
            return [[""]]
        try:
            parsed = json.loads(stripped)
        except Exception:
            # Support comma-separated fallback if user passed "a, b, c" instead of valid JSON
            if stripped.startswith("[") and stripped.endswith("]"):
                inner = stripped[1:-1].strip()
                parsed = [x.strip().strip("'\"") for x in inner.split(",")] if inner else []
            else:
                parsed = [[stripped]]

    if isinstance(parsed, list):
        if not parsed:
            return [[]]
        if all(isinstance(row, list) for row in parsed):
            return parsed
        return [parsed]

    return [[parsed]]


class GoogleSheetsClient:
    """Async Google Drive v3 + Google Sheets v4 client authenticated via OAuth2 refresh token."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        folder_id: Optional[str] = None,
    ):
        self.client_id = (client_id or settings.GOOGLE_DRIVE_OAUTH_CLIENT_ID or "").strip()
        self.client_secret = (client_secret or settings.GOOGLE_DRIVE_OAUTH_CLIENT_SECRET or "").strip()
        self.refresh_token = (refresh_token or settings.GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN or "").strip()
        self.folder_id = (folder_id or settings.GOOGLE_DRIVE_FOLDER_ID or "").strip() or None
        self._access_token: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    async def get_access_token(self, http_client: httpx.AsyncClient) -> str:
        """Exchange OAuth2 refresh_token for a short-lived Bearer access_token."""
        if self._access_token:
            return self._access_token

        if not self.is_configured():
            raise ScraperError(
                "google oauth not configured",
                "Missing GOOGLE_DRIVE_OAUTH_CLIENT_ID, GOOGLE_DRIVE_OAUTH_CLIENT_SECRET, or GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN in environment variables.",
                status_code=409,
            )

        resp = await http_client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            detail = resp.text[:300]
            try:
                err_j = resp.json()
                detail = err_j.get("error_description") or err_j.get("error") or detail
            except Exception:
                pass
            raise ScraperError(
                "google oauth token error",
                f"Failed to exchange Google OAuth refresh token (HTTP {resp.status_code}): {detail}",
                status_code=401,
            )

        token_data = resp.json() or {}
        access_token = token_data.get("access_token")
        if not access_token:
            raise ScraperError("google oauth token error", "Google OAuth response did not include an access_token.", status_code=401)
        self._access_token = str(access_token)
        return self._access_token

    async def list_spreadsheets(self, folder_id: Optional[str] = None) -> Dict[str, Any]:
        """List all spreadsheets in the configured Google Drive folder (or across Drive if no folder set)."""
        eff_folder = (folder_id or self.folder_id or "").strip() or None
        async with httpx.AsyncClient(timeout=25.0) as http_client:
            token = await self.get_access_token(http_client)
            headers = {"Authorization": f"Bearer {token}"}

            q_parts = ["mimeType='application/vnd.google-apps.spreadsheet'", "trashed=false"]
            if eff_folder:
                q_parts.insert(0, f"'{eff_folder}' in parents")

            params = {
                "q": " and ".join(q_parts),
                "fields": "files(id,name,modifiedTime,webViewLink)",
                "orderBy": "modifiedTime desc",
                "pageSize": 100,
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            }
            resp = await http_client.get(
                "https://www.googleapis.com/drive/v3/files",
                params=params,
                headers=headers,
            )
            if resp.status_code != 200:
                raise ScraperError(
                    "google drive list error",
                    f"Google Drive API error (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=502,
                )

            files = (resp.json() or {}).get("files") or []
            spreadsheets = [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "modified_time": f.get("modifiedTime"),
                    "url": f.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{f.get('id')}/edit",
                }
                for f in files
            ]
            return {
                "status": "ok",
                "folder_id": eff_folder,
                "count": len(spreadsheets),
                "spreadsheets": spreadsheets,
            }

    async def _resolve_spreadsheet_id(self, http_client: httpx.AsyncClient, name_or_id: str) -> Tuple[str, Optional[str]]:
        """Resolve a spreadsheet name, URL, or ID to (spreadsheet_id, matched_name)."""
        clean = (name_or_id or "").strip()
        if not clean:
            raise ScraperError("missing sheet identifier", "Provide a spreadsheet name or ID.", status_code=400)

        url_id = extract_spreadsheet_id_from_url(clean)
        if url_id:
            return url_id, None

        # Search spreadsheets in configured folder (and fallback to global Drive search)
        token = await self.get_access_token(http_client)
        headers = {"Authorization": f"Bearer {token}"}

        for search_folder in ([self.folder_id, None] if self.folder_id else [None]):
            q_parts = ["mimeType='application/vnd.google-apps.spreadsheet'", "trashed=false"]
            if search_folder:
                q_parts.insert(0, f"'{search_folder}' in parents")
            resp = await http_client.get(
                "https://www.googleapis.com/drive/v3/files",
                params={
                    "q": " and ".join(q_parts),
                    "fields": "files(id,name)",
                    "pageSize": 100,
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                },
                headers=headers,
            )
            if resp.status_code == 200:
                files = (resp.json() or {}).get("files") or []
                # 1. Exact ID match
                for f in files:
                    if f.get("id") == clean:
                        return f["id"], f.get("name")
                # 2. Exact case-insensitive name match
                for f in files:
                    if (f.get("name") or "").strip().lower() == clean.lower():
                        return f["id"], f.get("name")
                # 3. Case-insensitive substring match
                for f in files:
                    if clean.lower() in (f.get("name") or "").lower():
                        return f["id"], f.get("name")

        # If it looks like a raw Google Drive file ID (alphanumeric/hyphen/underscore, no spaces, >= 20 chars), use it directly
        if re.match(r"^[A-Za-z0-9_-]{20,}$", clean):
            return clean, None

        raise ScraperError(
            "spreadsheet not found",
            f"Could not find a Google Sheet matching '{clean}' in Drive folder '{self.folder_id or 'root'}'. Run 'sheets list' to see available spreadsheets.",
            status_code=404,
        )

    async def _fetch_sheet_metadata(self, http_client: httpx.AsyncClient, spreadsheet_id: str) -> Dict[str, Any]:
        """Fetch spreadsheet title and worksheet tab names."""
        token = await self.get_access_token(http_client)
        resp = await http_client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "spreadsheetId,properties.title,sheets.properties(sheetId,title,index,gridProperties)"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            raise ScraperError("spreadsheet not found", f"Spreadsheet '{spreadsheet_id}' not found.", status_code=404)
        if resp.status_code != 200:
            raise ScraperError(
                "google sheets error",
                f"Google Sheets metadata error (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=502,
            )
        data = resp.json() or {}
        title = (data.get("properties") or {}).get("title")
        tabs = [
            (s.get("properties") or {}).get("title")
            for s in (data.get("sheets") or [])
            if (s.get("properties") or {}).get("title")
        ]
        return {"spreadsheet_id": spreadsheet_id, "title": title, "tabs": tabs}

    async def read_sheet(self, name_or_id: str, range_str: Optional[str] = None) -> Dict[str, Any]:
        """Read data from a spreadsheet (or specific range like 'A1:E20' or 'Sheet1!A1:E20')."""
        async with httpx.AsyncClient(timeout=25.0) as http_client:
            spreadsheet_id, matched_name = await self._resolve_spreadsheet_id(http_client, name_or_id)
            meta = await self._fetch_sheet_metadata(http_client, spreadsheet_id)
            tabs = meta.get("tabs") or []
            default_tab = tabs[0] if tabs else "Sheet1"
            effective_range = (range_str or "").strip() or default_tab

            token = await self.get_access_token(http_client)
            encoded_range = urllib.parse.quote(effective_range, safe="!:$'")
            resp = await http_client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
                params={"valueRenderOption": "FORMATTED_VALUE"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                raise ScraperError(
                    "google sheets read error",
                    f"Failed to read range '{effective_range}' from '{meta.get('title') or spreadsheet_id}' (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=400 if resp.status_code == 400 else 502,
                )

            payload = resp.json() or {}
            rows: List[List[Any]] = payload.get("values") or []
            return {
                "status": "ok",
                "spreadsheet_id": spreadsheet_id,
                "title": meta.get("title") or matched_name,
                "sheets": tabs,
                "range": payload.get("range") or effective_range,
                "row_count": len(rows),
                "values": rows,
            }

    async def update_sheet(self, name_or_id: str, range_str: str, raw_values: Any) -> Dict[str, Any]:
        """Write values to a specific cell or range (e.g. 'A1:C2' or 'Sheet1!B5')."""
        clean_range = (range_str or "").strip()
        if not clean_range:
            raise ScraperError("missing range", "Provide a target cell or range (e.g. 'A1:D2' or 'B5').", status_code=400)

        values_2d = normalize_values_payload(raw_values)
        async with httpx.AsyncClient(timeout=25.0) as http_client:
            spreadsheet_id, matched_name = await self._resolve_spreadsheet_id(http_client, name_or_id)
            token = await self.get_access_token(http_client)
            encoded_range = urllib.parse.quote(clean_range, safe="!:$'")
            resp = await http_client.put(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
                params={"valueInputOption": "USER_ENTERED"},
                json={
                    "range": clean_range,
                    "majorDimension": "ROWS",
                    "values": values_2d,
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                raise ScraperError(
                    "google sheets update error",
                    f"Failed to update range '{clean_range}' (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=400 if resp.status_code == 400 else 502,
                )

            res = resp.json() or {}
            return {
                "status": "updated",
                "spreadsheet_id": spreadsheet_id,
                "title": matched_name,
                "updated_range": res.get("updatedRange") or clean_range,
                "updated_rows": res.get("updatedRows", len(values_2d)),
                "updated_columns": res.get("updatedColumns"),
                "updated_cells": res.get("updatedCells"),
            }

    async def append_sheet(self, name_or_id: str, raw_values: Any, range_str: Optional[str] = None) -> Dict[str, Any]:
        """Append one or more rows to a spreadsheet."""
        values_2d = normalize_values_payload(raw_values)
        async with httpx.AsyncClient(timeout=25.0) as http_client:
            spreadsheet_id, matched_name = await self._resolve_spreadsheet_id(http_client, name_or_id)
            target_range = (range_str or "").strip()
            if not target_range:
                meta = await self._fetch_sheet_metadata(http_client, spreadsheet_id)
                tabs = meta.get("tabs") or []
                target_range = tabs[0] if tabs else "Sheet1"
                matched_name = meta.get("title") or matched_name

            token = await self.get_access_token(http_client)
            encoded_range = urllib.parse.quote(target_range, safe="!:$'")
            resp = await http_client.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}:append",
                params={
                    "valueInputOption": "USER_ENTERED",
                    "insertDataOption": "INSERT_ROWS",
                },
                json={
                    "majorDimension": "ROWS",
                    "values": values_2d,
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                raise ScraperError(
                    "google sheets append error",
                    f"Failed to append row to '{target_range}' (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=400 if resp.status_code == 400 else 502,
                )

            res = resp.json() or {}
            updates = res.get("updates") or {}
            return {
                "status": "appended",
                "spreadsheet_id": spreadsheet_id,
                "title": matched_name,
                "table_range": res.get("tableRange"),
                "updated_range": updates.get("updatedRange"),
                "updated_rows": updates.get("updatedRows", len(values_2d)),
                "updated_cells": updates.get("updatedCells"),
            }
