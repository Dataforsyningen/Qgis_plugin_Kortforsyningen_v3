from builtins import str
import codecs
import os.path
import datetime
import traceback
import json
import hashlib
import glob
from qgis.gui import QgsMessageBar
from qgis.core import *
from qgis.PyQt.QtCore import (
    QCoreApplication,
    QFileInfo,
    QFile,
    QUrl,
    QSettings,
    QTranslator,
    qVersion,
    QIODevice,
)
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkRequest
from qgis.PyQt.QtWidgets import QAction, QMenu, QPushButton
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt import QtCore, QtXml
from .qlr_file import QlrFile

FILE_MAX_AGE = datetime.timedelta(hours=12)
KF_SERVICES_URL = (
    "https://api.dataforsyningen.dk/service?request=GetServices&token={{kf_token}}"
)


def log_message(message):
    QgsMessageLog.logMessage(message, "Kortforsyningen plugin")


class KfConfig(QtCore.QObject):

    kf_con_error = QtCore.pyqtSignal()
    kf_settings_warning = QtCore.pyqtSignal()
    loaded = QtCore.pyqtSignal()

    def __init__(self, settings):
        super(KfConfig, self).__init__()
        self.settings = settings
        self.cached_kf_qlr_filename = None
        self.allowed_kf_services = {}
        self.kf_qlr_file = None
        self.background_category = None
        self.categories = None

        # Network
        self._services_network_manager = QNetworkAccessManager()
        self._qlr_network_manager = QNetworkAccessManager()
        self._services_network_manager.finished.connect(self._handle_services_response)
        self._qlr_network_manager.finished.connect(self._handle_qlr_response)

    def begin_load(self):
        self.cached_kf_qlr_filename = (
            self.settings.value("cache_path")
            + hashlib.md5(self.settings.value("token").encode()).hexdigest()
            + "_kortforsyning_data.qlr"
        )
        self.allowed_kf_services = {}
        if self.settings.is_set():
            try:
                self._request_services()
            except Exception as e:
                log_message(traceback.format_exc())
                self.kf_con_error.emit()
                self.background_category = None
                self.categories = []
            self.debug_write_allowed_services()
        else:
            self.kf_settings_warning.emit()
            self.background_category = None
            self.categories = []

    def _request_services(self):
        url_to_get = self.insert_token(KF_SERVICES_URL)
        self._services_network_manager.get(QNetworkRequest(QUrl(url_to_get)))

    def _handle_services_response(self, network_reply):
        if network_reply.error():
            self.background_category = None
            self.categories = []
            self.kf_con_error.emit()
            log_message(
                f"Network error getting services from kf. Error code : "
                + str(network_reply.error())
            )
            return
        response = str(network_reply.readAll(), "utf-8")
        doc = QtXml.QDomDocument()
        doc.setContent(response)
        service_types = doc.documentElement().childNodes()
        i = 0
        allowed = {}
        allowed["any_type"] = {"services": []}
        while i < service_types.count():
            service_type = service_types.at(i)
            service_type_name = service_type.nodeName()
            allowed[service_type_name] = {"services": []}
            services = service_type.childNodes()
            j = 0
            while j < services.count():
                service = services.at(j)
                service_name = service.nodeName()
                allowed[service_type_name]["services"].append(service_name)
                allowed["any_type"]["services"].append(service_name)
                j = j + 1
            i = i + 1
        self.allowed_kf_services = allowed
        if not allowed["any_type"]["services"]:
            self.kf_con_error.emit()
            log_message(
                f"Kortforsyningen returned an empty list of allowed services for token: {self.settings.value('token')}"
            )
        # Go on and get QLR
        self._get_qlr_file()

    def _get_qlr_file(self):
        local_file_exists = os.path.exists(self.cached_kf_qlr_filename)
        if local_file_exists:
            local_file_time = datetime.datetime.fromtimestamp(
                os.path.getmtime(self.cached_kf_qlr_filename)
            )
            use_cached = local_file_time > datetime.datetime.now() - FILE_MAX_AGE
            if use_cached:
                # Skip requesting remote qlr
                self._load_config_from_cached_kf_qlr()
                return
        # Get qlr from KF
        self._request_kf_qlr_file()

    def _request_kf_qlr_file(self):
        url_to_get = self.settings.value("kf_qlr_url")
        self._qlr_network_manager.get(QNetworkRequest(QUrl(url_to_get)))

    def _handle_qlr_response(self, network_reply):
        if network_reply.error():
            log_message(
                "No contact to the configuration at "
                + self.settings.value("kf_qlr_url")
                + ". Error code : "
                + str(network_reply.error())
            )
        else:
            response = str(network_reply.readAll(), "utf-8")
            response = self.insert_token(response)
            self.write_cached_kf_qlr(response)
        # Now load and use it
        self._load_config_from_cached_kf_qlr()

    def _load_config_from_cached_kf_qlr(self):
        self.kf_qlr_file = QlrFile(self._read_cached_kf_qlr())
        self.background_category, self.categories = self.get_kf_categories()
        self.loaded.emit()

    def get_categories(self):
        return self.categories

    def get_background_category(self):
        return self.background_category

    def get_maplayer_node(self, id):
        return self.kf_qlr_file.get_maplayer_node(id)

    def get_kf_categories(self):
        kf_categories = []
        kf_background_category = None
        groups_with_layers = self.kf_qlr_file.get_groups_with_layers()
        for group in groups_with_layers:
            kf_category = {"name": group["name"], "selectables": []}
            for layer in group["layers"]:
                if self.user_has_access(layer["service"]):
                    kf_category["selectables"].append(
                        {
                            "type": "layer",
                            "source": "kf",
                            "name": layer["name"],
                            "id": layer["id"],
                        }
                    )
            if len(kf_category["selectables"]) > 0:
                kf_categories.append(kf_category)
                if group["name"] == "Baggrundskort":
                    kf_background_category = kf_category
        return kf_background_category, kf_categories

    def user_has_access(self, service_name):
        return service_name in self.allowed_kf_services["any_type"]["services"]

    def get_custom_categories(self):
        return []

    def _read_cached_kf_qlr(self):
        # return file(unicode(self.cached_kf_qlr_filename)).read()
        f = QFile(self.cached_kf_qlr_filename)
        f.open(QIODevice.ReadOnly)
        return f.readAll()

    def write_cached_kf_qlr(self, contents):
        """We only call this function IF we have a new version downloaded"""
        # Remove old versions file
        for filename in glob.glob(
            self.settings.value("cache_path") + "*_kortforsyning_data.qlr"
        ):
            os.remove(filename)

        # Write new version
        with codecs.open(self.cached_kf_qlr_filename, "w", "utf-8") as f:
            f.write(contents)

    def debug_write_allowed_services(self):
        try:
            debug_filename = (
                self.settings.value("cache_path")
                + self.settings.value("username")
                + ".txt"
            )
            if os.path.exists(debug_filename):
                os.remove(debug_filename)
            with codecs.open(debug_filename, "w", "utf-8") as f:
                f.write(
                    json.dumps(
                        self.allowed_kf_services["any_type"]["services"], indent=2
                    )
                    .replace("[", "")
                    .replace("]", "")
                )
        except Exception:
            pass

    def insert_token(self, text):
        result = text
        replace_vars = {}
        replace_vars["kf_token"] = self.settings.value("token")
        for i, j in replace_vars.items():
            result = result.replace("{{" + str(i) + "}}", str(j))
        return result
