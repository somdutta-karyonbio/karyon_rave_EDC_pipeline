"""
Medidata RAVE Web Services REST client.

Endpoints used:
  GET /studies                          → list studies
  GET /studies/{studyOID}/subjects      → subject list (ODM XML)
  GET /datasets/{datasetName}           → clinical dataset (ODM XML or JSON)

Authentication: HTTP Basic (username / password).
All responses are parsed from ODM-XML into flat list-of-dicts,
which are then written to staging tables by extract.py.
"""

import logging
import os
import time
from typing import Any
from xml.etree import ElementTree as ET

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"
MDSOL_NS = "http://www.mdsol.com/ns/odm/metadata"


def _load_config(config_path: str = "config/config.yaml") -> dict:
    import re
    with open(config_path) as f:
        raw = f.read()
    def _sub(m):
        key = m.group(1) or m.group(2)
        return os.environ.get(key, m.group(0))
    raw = re.sub(r'\$\{(\w+)\}|\$(\w+)', _sub, raw)
    return yaml.safe_load(raw)["rave"]


class RaveClient:
    def __init__(self, config_path: str = "config/config.yaml"):
        cfg = _load_config(config_path)
        self.base_url = cfg["base_url"].rstrip("/")
        self.auth = (cfg["username"], cfg["password"])
        self.study_oid = cfg["study_oid"]
        self.timeout = cfg.get("timeout", 30)
        self.datasets = cfg.get("datasets", {})
        self._session = self._build_session(
            retries=cfg.get("retry_attempts", 3),
            backoff=cfg.get("retry_backoff", 2),
        )

    # ── HTTP session ─────────────────────────────────────────

    @staticmethod
    def _build_session(retries: int, backoff: float) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        logger.debug("RAVE GET %s params=%s", url, params)
        resp = self._session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    # ── ODM-XML parsers ──────────────────────────────────────

    @staticmethod
    def _ns(tag: str) -> str:
        return f"{{{ODM_NS}}}{tag}"

    def _parse_odm_subjects(self, xml_text: str) -> list[dict]:
        """Parse ODM XML SubjectData into list of dicts."""
        root = ET.fromstring(xml_text)
        subjects = []
        for clinical_data in root.iter(self._ns("ClinicalData")):
            for subject_data in clinical_data.iter(self._ns("SubjectData")):
                subj = {"SubjectKey": subject_data.attrib.get("SubjectKey", "")}
                for site_ref in subject_data.iter(self._ns("SiteRef")):
                    subj["SiteOID"] = site_ref.attrib.get("LocationOID", "")
                subjects.append(subj)
        return subjects

    def _parse_odm_dataset(self, xml_text: str, dataset_name: str) -> list[dict]:
        """Parse a full ODM clinical dataset into list of flat row dicts."""
        root = ET.fromstring(xml_text)
        rows = []
        for clinical_data in root.iter(self._ns("ClinicalData")):
            for subject_data in clinical_data.iter(self._ns("SubjectData")):
                subject_key = subject_data.attrib.get("SubjectKey", "")
                site_oid = ""
                for site_ref in subject_data.iter(self._ns("SiteRef")):
                    site_oid = site_ref.attrib.get("LocationOID", "")
                for study_event in subject_data.iter(self._ns("StudyEventData")):
                    event_oid = study_event.attrib.get("StudyEventOID", "")
                    event_repeat = study_event.attrib.get("StudyEventRepeatKey", "1")
                    for form_data in study_event.iter(self._ns("FormData")):
                        form_oid = form_data.attrib.get("FormOID", "")
                        for item_group in form_data.iter(self._ns("ItemGroupData")):
                            row: dict[str, Any] = {
                                "SubjectKey": subject_key,
                                "SiteOID": site_oid,
                                "StudyEventOID": event_oid,
                                "StudyEventRepeatKey": event_repeat,
                                "FormOID": form_oid,
                            }
                            for item_data in item_group.iter(self._ns("ItemData")):
                                item_oid = item_data.attrib.get("ItemOID", "")
                                value = item_data.attrib.get("Value", "")
                                row[item_oid] = value
                            rows.append(row)
        logger.debug("Parsed %d rows from dataset '%s'", len(rows), dataset_name)
        return rows

    # ── Public API methods ───────────────────────────────────

    def ping(self) -> bool:
        """Check RAVE connectivity."""
        try:
            self._get("version")
            return True
        except Exception as exc:
            logger.error("RAVE ping failed: %s", exc)
            return False

    def list_studies(self) -> list[dict]:
        resp = self._get("studies")
        root = ET.fromstring(resp.text)
        studies = []
        for study in root.iter(self._ns("Study")):
            studies.append({
                "oid": study.attrib.get("OID", ""),
                "name": study.attrib.get("StudyName", ""),
            })
        return studies

    def get_subjects(self) -> list[dict]:
        path = f"studies/{self.study_oid}/subjects"
        resp = self._get(path)
        return self._parse_odm_subjects(resp.text)

    def get_dataset(self, dataset_name: str) -> list[dict]:
        """Fetch a named RAVE dataset and return as list of row dicts."""
        path = f"datasets/{dataset_name}"
        resp = self._get(path, params={"studyoid": self.study_oid})
        return self._parse_odm_dataset(resp.text, dataset_name)

    def get_all_datasets(self) -> dict[str, list[dict]]:
        """Fetch all configured datasets. Returns {domain: [rows]}."""
        results: dict[str, list[dict]] = {}
        for domain, dataset_name in self.datasets.items():
            logger.info("Fetching RAVE dataset: %s (%s)", domain, dataset_name)
            try:
                results[domain] = self.get_dataset(dataset_name)
            except requests.HTTPError as exc:
                logger.error("Failed to fetch dataset '%s': %s", domain, exc)
                results[domain] = []
            time.sleep(0.5)  # be polite to RAVE
        return results
