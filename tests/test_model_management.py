"""Regression tests for versioned model upload and runtime selection."""

import io
import base64
import json
from contextlib import nullcontext
import os
import pathlib
import pickle
import re
import socket
import tempfile
import unittest
import warnings
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image
from sqlalchemy import inspect, select


TEST_WORKSPACE = tempfile.TemporaryDirectory()
os.environ["PLANKTONLAB_DATA_DIR"] = TEST_WORKSPACE.name
os.environ["SQLITE_DATABASE_PATH"] = str(Path(TEST_WORKSPACE.name) / "test.db")
os.environ["MODEL_STORAGE_FOLDER"] = str(Path(TEST_WORKSPACE.name) / "models")

with patch("fastai.learner.load_learner", return_value=None):
    from apps import app
    from apps.static.assets.utils.database_classes import (
        ModelRun,
        ModelVersion,
        Review,
        Samples,
        Sampling_metadata,
        User,
        Lakes,
        build_species_zip,
        db,
        initialize_database,
        start_model_run,
    )
    from apps import views
    from apps.static.assets.utils import image_class
    from apps.static.assets.utils import lake_metadata
    from apps.static.assets.utils.lake_metadata import LakeLookupUnavailable
    from apps.static.assets.utils.image_class import Image_Classifier
    from run import environment_flag, port_is_available


class ModelManagementTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = app.test_client()
        views._model_learner_cache.clear()
        with app.app_context():
            db.drop_all()
            initialize_database()

    def test_runtime_uses_portable_data_directory_and_detects_busy_ports(self):
        self.assertEqual(Path(app.config["DATA_DIRECTORY"]), Path(TEST_WORKSPACE.name))
        self.assertEqual(
            Path(app.config["UPLOAD_FOLDER"]), Path(TEST_WORKSPACE.name) / "uploads"
        )

        with patch.dict(os.environ, {"PLANKTONLAB_TEST_FLAG": "YES"}):
            self.assertTrue(environment_flag("PLANKTONLAB_TEST_FLAG"))

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.assertFalse(port_is_available("127.0.0.1", listener.getsockname()[1]))

    def test_data_explorer_returns_more_than_500_rows(self):
        with app.app_context():
            db.session.add_all(
                User(username=f"explorer-{index}", email=f"explorer-{index}@example.org")
                for index in range(505)
            )
            db.session.commit()

        page = self.client.get("/pages/data_explorer")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"All matching rows are returned", page.data)
        self.assertNotIn(b"Maximum 500 returned rows", page.data)

        sql_response = self.client.post(
            "/pages/run_sql_query",
            json={"query": 'SELECT id FROM "user" ORDER BY id'},
        )
        self.assertEqual(sql_response.status_code, 200)
        self.assertEqual(sql_response.get_json()["row_count"], 505)
        self.assertNotIn("limit", sql_response.get_json())
        self.assertNotIn("truncated", sql_response.get_json())

        visual_response = self.client.post(
            "/pages/run_query",
            json={
                "base_table": "user",
                "joins": [],
                "columns": {"user": ["id"]},
                "conditions": [],
            },
        )
        self.assertEqual(visual_response.status_code, 200)
        self.assertEqual(len(visual_response.get_json()), 505)

    def test_lake_entry_forms_use_country_dropdowns(self):
        setup_response = self.client.get("/setup")
        add_response = self.client.get("/add_data/lakes")

        self.assertEqual(setup_response.status_code, 200)
        self.assertIn(b'<select id="country" name="country" required>', setup_response.data)
        self.assertIn(b'<option value="Germany"', setup_response.data)
        self.assertEqual(add_response.status_code, 200)
        self.assertIn(b'<select name="country" id="country" required>', add_response.data)
        self.assertIn(b'<option value="Switzerland"', add_response.data)
        self.assertIn(b'data-lake-lookup', setup_response.data)
        self.assertIn(b'Search Wikidata', add_response.data)
        depth_input = re.search(br'<input[^>]+id="depth"[^>]*>', setup_response.data)
        self.assertIsNotNone(depth_input)
        self.assertNotIn(b"required", depth_input.group())

    def test_setup_accepts_minimal_manual_lake_metadata(self):
        response = self.client.post(
            "/setup",
            data={
                "username": "Manual Operator",
                "email": "manual@example.org",
                "lake": "Offline Lake",
                "country": "Switzerland",
            },
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            lake = db.session.execute(
                select(Lakes).where(Lakes.lake == "Offline Lake")
            ).scalar_one()
            self.assertIsNone(lake.depth)
            self.assertIsNone(lake.area)
            self.assertIsNone(lake.external_source)

    def test_lake_lookup_endpoint_returns_candidates_and_offline_fallback(self):
        candidate = {
            "lake": "Walchensee",
            "country": "Germany",
            "external_source": "Wikidata",
            "external_id": "Q174645",
        }
        with patch.object(views, "search_lakes", return_value=[candidate]):
            response = self.client.get(
                "/api/lakes/search?name=Walchensee&country=Germany"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"], [candidate])
        self.assertTrue(response.get_json()["manual_entry"])

        with patch.object(
            views,
            "search_lakes",
            side_effect=LakeLookupUnavailable("Lookup unavailable."),
        ):
            response = self.client.get("/api/lakes/search?name=Walchensee")

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["manual_entry"])

    def test_lake_lookup_reports_system_trust_store_tls_failures(self):
        with patch.object(
            lake_metadata.requests,
            "get",
            side_effect=lake_metadata.requests.exceptions.SSLError(
                "unknown issuer"
            ),
        ):
            with self.assertRaisesRegex(
                LakeLookupUnavailable,
                "system trust store",
            ):
                lake_metadata.search_lakes("Walchensee")

    def test_wikidata_lake_metadata_is_normalized(self):
        def claim(value):
            return [{
                "rank": "normal",
                "mainsnak": {
                    "snaktype": "value",
                    "datavalue": {"value": value},
                },
            }]

        responses = []
        for payload in (
            {"search": [{"id": "QLAKE"}, {"id": "QSHIP"}]},
            {
                "entities": {
                    "QLAKE": {
                        "labels": {"en": {"value": "Test Lake"}},
                        "descriptions": {"en": {"value": "lake in Example Region"}},
                        "claims": {
                            "P31": claim({"id": "Q23397"}),
                            "P17": claim({"id": "QCOUNTRY"}),
                            "P131": claim({"id": "QREGION"}),
                            "P625": claim({"latitude": 47.5, "longitude": 8.5}),
                            "P4511": claim({
                                "amount": "+42.5",
                                "unit": "http://www.wikidata.org/entity/Q11573",
                            }),
                            "P2046": claim({
                                "amount": "+12.75",
                                "unit": "http://www.wikidata.org/entity/Q712226",
                            }),
                        },
                    },
                    "QSHIP": {
                        "labels": {"en": {"value": "Test Lake"}},
                        "descriptions": {"en": {"value": "research vessel"}},
                        "claims": {},
                    },
                }
            },
            {
                "entities": {
                    "Q23397": {"labels": {"en": {"value": "lake"}}},
                    "QCOUNTRY": {"labels": {"en": {"value": "Switzerland"}}},
                    "QREGION": {"labels": {"en": {"value": "Example Region"}}},
                }
            },
        ):
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = payload
            responses.append(response)

        with patch.object(lake_metadata.requests, "get", side_effect=responses):
            candidates = lake_metadata.search_lakes(
                "Test Lake", country="Switzerland"
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["external_id"], "QLAKE")
        self.assertEqual(candidates[0]["depth"], 42.5)
        self.assertEqual(candidates[0]["area"], 12.75)
        self.assertEqual(candidates[0]["administrative_region"], "Example Region")

    def test_database_initialization_creates_only_the_current_schema(self):
        with app.app_context():
            initialize_database()
            inspector = inspect(db.engine)
            self.assertNotIn("schema_migrations", inspector.get_table_names())
            self.assertEqual(
                set(inspector.get_view_names()),
                {"classification_summary", "model_run_summary", "review_queue"},
            )
            lake_columns = {column["name"] for column in inspector.get_columns("lakes")}
            self.assertIn("latitude", lake_columns)

    def test_lake_country_dropdown_preserves_custom_value_when_editing(self):
        with app.app_context():
            lake = Lakes(
                lake="Custom Lake",
                country="Custom region",
                depth=10,
                area=20,
            )
            db.session.add(lake)
            db.session.commit()
            lake_id = lake.id

        response = self.client.get(f"/edit/lakes/{lake_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'<option value="Custom region" selected>Custom region</option>',
            response.data,
        )

    def test_classifier_startup_does_not_probe_for_a_bundled_model(self):
        with patch.object(image_class, "load_learner") as load_learner_mock:
            classifier = Image_Classifier()

        load_learner_mock.assert_not_called()
        self.assertIsNone(classifier.learner)

    def test_model_artifacts_must_be_inside_managed_storage(self):
        external_artifact = Path(TEST_WORKSPACE.name) / "external-classifier.pkl"
        external_artifact.write_bytes(b"external")
        model_version = ModelVersion(
            name="external-model",
            version="external123",
            artifact_path=str(external_artifact),
        )

        with self.assertRaisesRegex(
            views.ModelArtifactError,
            "outside model storage",
        ):
            views.resolve_model_artifact(model_version)

    def test_model_loader_suppresses_only_the_fastai_pickle_warning(self):
        expected_learner = object()

        def warning_loader(*_args, **_kwargs):
            warnings.warn(
                "load_learner` uses Python's insecure pickle module, trusted files only.",
                UserWarning,
            )
            warnings.warn("Another model warning", RuntimeWarning)
            return expected_learner

        classifier = Image_Classifier.__new__(Image_Classifier)
        with patch(
            "apps.static.assets.utils.image_class.load_learner",
            side_effect=warning_loader,
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                learner = classifier.load_learner_from_file("trusted.pkl")

        self.assertIs(learner, expected_learner)
        self.assertEqual([str(item.message) for item in caught], ["Another model warning"])

    def test_model_loader_maps_foreign_path_class_only_during_load(self):
        classifier = Image_Classifier.__new__(Image_Classifier)
        foreign_path_name = "PosixPath" if os.name == "nt" else "WindowsPath"
        native_path = pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath
        original_foreign_path = getattr(pathlib, foreign_path_name)

        class ForeignPathReference:
            def __reduce__(self):
                return original_foreign_path, ("foreign/model.pkl",)

        foreign_path_pickle = pickle.dumps(ForeignPathReference())
        with self.assertRaises(NotImplementedError):
            pickle.loads(foreign_path_pickle)

        def assert_portable_path_mapping(model_path, cpu):
            self.assertEqual(model_path, Path("trusted.pkl"))
            self.assertTrue(cpu)
            self.assertIs(getattr(pathlib, foreign_path_name), native_path)
            return pickle.loads(foreign_path_pickle)

        with patch.object(
            image_class,
            "load_learner",
            side_effect=assert_portable_path_mapping,
        ):
            learner = classifier.load_learner_from_file(
                "trusted.pkl",
                raise_errors=True,
            )

        self.assertIsInstance(learner, native_path)
        self.assertIs(getattr(pathlib, foreign_path_name), original_foreign_path)

    def test_model_loader_restores_foreign_path_class_after_failure(self):
        classifier = Image_Classifier.__new__(Image_Classifier)
        foreign_path_name = "PosixPath" if os.name == "nt" else "WindowsPath"
        original_foreign_path = getattr(pathlib, foreign_path_name)

        with patch.object(
            image_class,
            "load_learner",
            side_effect=RuntimeError("invalid model"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid model"):
                classifier.load_learner_from_file(
                    "trusted.pkl",
                    raise_errors=True,
                )

        self.assertIs(getattr(pathlib, foreign_path_name), original_foreign_path)

    def test_prediction_suppresses_fastai_progress_bar(self):
        classifier = Image_Classifier.__new__(Image_Classifier)
        classifier.orig_img = np.zeros((8, 8), dtype=np.uint8)
        classifier.meta_id = 1
        learner = Mock()
        learner.no_bar.return_value = nullcontext()
        probabilities = Mock()
        probabilities.max.return_value.item.return_value = 0.75
        learner.predict.return_value = ("Daphnia", None, probabilities)

        result = classifier.coords_to_base64_with_classification(
            [0, 0, 4, 4],
            learner=learner,
        )

        learner.no_bar.assert_called_once_with()
        learner.predict.assert_called_once()
        self.assertEqual(result["species"], "Daphnia")

    def test_save_boxes_streams_progress_for_each_crop(self):
        with app.app_context():
            user = User(username="stream-user", email="stream@example.com")
            lake = Lakes(lake="Stream Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name="stream.jpg",
                sampling_date=date(2026, 9, 11),
                image_path="stream.jpg",
            )
            db.session.add(source)
            db.session.commit()
            source_id = source.id

        views.tmp_image.meta_id = source_id
        boxes = [[0, 0, 4, 4], [4, 0, 4, 4]]

        def classified_crop(box, *_args, **_kwargs):
            return {
                "sampling_metadata_id": source_id,
                "x": box[0],
                "y": box[1],
                "w": box[2],
                "h": box[3],
                "species": "Unlabeled",
                "confidence": 0.0,
                "image_base64": "sample",
            }

        with patch.object(
            views.tmp_image,
            "coords_to_base64_with_classification",
            side_effect=classified_crop,
        ):
            response = self.client.post(
                "/save_boxes",
                json={"boxes": boxes},
                headers={"Accept": "application/x-ndjson"},
                buffered=True,
            )

        events = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/x-ndjson")
        self.assertEqual(
            [(event.get("phase"), event.get("current")) for event in events[:-1]],
            [
                ("classifying", 0),
                ("classified", 1),
                ("classifying", 1),
                ("classified", 2),
            ],
        )
        self.assertEqual(events[-1]["status"], "success")
        self.assertEqual(events[-1]["total"], 2)

        json_response = self.client.post("/save_boxes", json={"boxes": []})
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json_response.mimetype, "application/json")
        self.assertEqual(json_response.get_json()["status"], "success")

    def test_save_changes_deletes_boxes_missing_from_visible_canvas(self):
        with app.app_context():
            user = User(username="delete-user", email="delete@example.com")
            lake = Lakes(lake="Delete Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name="delete.jpg",
                sampling_date=date(2026, 9, 11),
                image_path="delete.jpg",
            )
            db.session.add(source)
            db.session.flush()
            db.session.add_all(
                [
                    Samples(
                        sampling_metadata_id=source.id,
                        x=index * 4,
                        y=0,
                        w=4,
                        h=4,
                        species="Unlabeled",
                        confidence=0.0,
                        image_base64="sample",
                    )
                    for index in range(2)
                ]
            )
            db.session.commit()
            source_id = source.id

        views.tmp_image.meta_id = source_id
        response = self.client.post(
            "/update_boxes",
            json={"update_boxes": [], "boxes": []},
        )

        self.assertEqual(response.status_code, 200)
        with app.app_context():
            remaining = Samples.query.filter_by(sampling_metadata_id=source_id).count()
        self.assertEqual(remaining, 0)

    def test_uploads_are_stored_as_distinct_content_addressed_versions(self):
        with patch.object(
            views.tmp_image,
            "load_learner_from_file",
            return_value=object(),
        ):
            first_response = self.client.post(
                "/upload-model",
                data={
                    "model_name": "classifier-a",
                    "model_file": (io.BytesIO(b"first model"), "first.pkl"),
                },
                content_type="multipart/form-data",
            )
            second_response = self.client.post(
                "/upload-model",
                data={
                    "model_name": "classifier-b",
                    "model_file": (io.BytesIO(b"second model"), "second.pkl"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        with app.app_context():
            versions = ModelVersion.query.order_by(ModelVersion.id).all()
            self.assertEqual(len(versions), 2)
            self.assertFalse(versions[0].is_active)
            self.assertTrue(versions[1].is_active)
            paths = [views.resolve_model_artifact(version) for version in versions]
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(all(path.is_file() for path in paths))

    def test_model_selection_is_scoped_to_the_browser_session(self):
        artifact = Path(app.config["MODEL_STORAGE_FOLDER"]) / "selected.pkl"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"model")
        with app.app_context():
            version = ModelVersion(
                name="runtime-model",
                version="abc123",
                artifact_path=str(artifact),
                is_active=False,
            )
            db.session.add(version)
            db.session.commit()
            version_id = version.id

        with patch.object(views, "load_model_version", return_value=object()):
            response = self.client.post(
                "/models/select",
                data={"model_version_id": version_id, "next": "/pages/training"},
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as browser_session:
            self.assertEqual(browser_session["selected_model_version_id"], version_id)

    def test_model_run_records_the_explicit_runtime_version(self):
        with app.app_context():
            user = User(username="tester", email="tester@example.com")
            lake = Lakes(lake="Test Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name="test.jpg",
                sampling_date=date(2026, 9, 8),
                image_path="test.jpg",
            )
            version = ModelVersion(
                name="provenance-model",
                version="def456",
                artifact_path="instance/models/def456.pkl",
                is_active=False,
            )
            db.session.add_all([source, version])
            db.session.flush()

            run = start_model_run(source.id, model_version=version)
            db.session.commit()

            persisted = db.session.get(ModelRun, run.id)
            self.assertEqual(persisted.model_version_id, version.id)

    def test_training_page_uses_task_focused_progressive_disclosure(self):
        response = self.client.get("/pages/training")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'aria-label="Training workflow"', response.data)
        self.assertIn(b'class="workspace-metrics"', response.data)
        self.assertIn(b'id="model-upload-trigger"', response.data)
        self.assertIn(b'id="model-upload-dialog"', response.data)
        self.assertIn(b'aria-labelledby="model-upload-title"', response.data)
        self.assertIn(b'class="model-upload-form"', response.data)
        self.assertIn(b'modelUploadDialog.showModal()', response.data)
        self.assertNotIn(b'class="upload-disclosure"', response.data)
        self.assertIn(b'class="model-library-disclosure"', response.data)
        self.assertIn(b'class="export-options"', response.data)
        self.assertIn(b'class="export-section export-optional"', response.data)

    def test_classification_toolbar_opens_first_unreviewed_taxon(self):
        image_path = Path(TEST_WORKSPACE.name) / "toolbar-review.png"
        Image.new("RGB", (24, 24), color="white").save(image_path)

        with app.app_context():
            user = User(username="toolbar-reviewer", email="toolbar@example.com")
            lake = Lakes(lake="Toolbar Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name=image_path.name,
                sampling_date=date(2026, 9, 9),
                image_path=str(image_path),
            )
            db.session.add(source)
            db.session.flush()
            db.session.add_all(
                [
                    Samples(
                        sampling_metadata_id=source.id,
                        x=0,
                        y=0,
                        w=10,
                        h=10,
                        species="Alpha",
                        confidence=0.9,
                        image_base64="sample",
                        review_status="reviewed",
                    ),
                    Samples(
                        sampling_metadata_id=source.id,
                        x=10,
                        y=0,
                        w=10,
                        h=10,
                        species="Beta",
                        confidence=0.8,
                        image_base64="sample",
                    ),
                ]
            )
            db.session.commit()
            source_id = source.id

        response = self.client.get(f"/pages/classification/{source_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="review-results"', response.data)
        self.assertIn(b'Review Results', response.data)
        self.assertIn(
            f"/pages/classification/{source_id}/Beta".encode(), response.data
        )
        self.assertNotIn(b'id="review-results" type="button" disabled', response.data)
        self.assertIn(b'id="classification-progress"', response.data)
        self.assertNotIn(b'class="loader"', response.data)
        self.assertIn(b"'Accept': 'application/x-ndjson'", response.data)
        self.assertIn(b"JSON.stringify({ update_boxes, boxes })", response.data)

    def test_bulk_review_accepts_only_the_current_species_and_preserves_provenance(self):
        with app.app_context():
            user = User(username="reviewer", email="reviewer@example.com")
            lake = Lakes(lake="Review Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name="review.jpg",
                sampling_date=date(2026, 9, 9),
                image_path="review.jpg",
            )
            version = ModelVersion(
                name="review-model",
                version="review123",
                artifact_path="instance/models/review123.pkl",
            )
            db.session.add_all([source, version])
            db.session.flush()
            run = start_model_run(source.id, model_version=version)
            accepted_samples = [
                Samples(
                    sampling_metadata_id=source.id,
                    model_run_id=run.id,
                    x=index * 10,
                    y=0,
                    w=10,
                    h=10,
                    species="Calanoida",
                    confidence=confidence,
                    image_base64="sample",
                )
                for index, confidence in enumerate((0.72, 0.84))
            ]
            other_sample = Samples(
                sampling_metadata_id=source.id,
                model_run_id=run.id,
                x=30,
                y=0,
                w=10,
                h=10,
                species="Daphnia",
                confidence=0.63,
                image_base64="sample",
            )
            db.session.add_all([*accepted_samples, other_sample])
            db.session.commit()
            source_id = source.id
            accepted_ids = [sample.id for sample in accepted_samples]
            other_id = other_sample.id
            run_id = run.id

        response = self.client.post(
            f"/review-species/{source_id}/Calanoida",
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            reviewed = [db.session.get(Samples, sample_id) for sample_id in accepted_ids]
            self.assertEqual([sample.review_status for sample in reviewed], ["reviewed"] * 2)
            self.assertEqual([sample.confidence for sample in reviewed], [0.72, 0.84])
            self.assertEqual([sample.model_run_id for sample in reviewed], [run_id] * 2)
            self.assertEqual(db.session.get(Samples, other_id).review_status, "unreviewed")
            decisions = Review.query.order_by(Review.id).all()
            self.assertEqual([decision.status for decision in decisions], ["accepted"] * 2)

    def test_review_gallery_corrections_and_next_taxon_navigation(self):
        with app.app_context():
            user = User(username="queue-reviewer", email="queue@example.com")
            lake = Lakes(lake="Queue Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            source = Sampling_metadata(
                user_id=user.id,
                lake_id=lake.id,
                image_name="queue.jpg",
                sampling_date=date(2026, 9, 9),
                image_path="queue.jpg",
            )
            db.session.add(source)
            db.session.flush()
            samples = [
                Samples(
                    sampling_metadata_id=source.id,
                    x=index * 10,
                    y=0,
                    w=10,
                    h=10,
                    species=species,
                    confidence=0.75,
                    image_base64="sample",
                )
                for index, species in enumerate(("Alpha", "Beta", "Delta"))
            ]
            db.session.add_all(samples)
            db.session.commit()
            source_id = source.id
            alpha_id, beta_id, _ = (sample.id for sample in samples)

        gallery = self.client.get(f"/pages/classification/{source_id}/Alpha")
        self.assertEqual(gallery.status_code, 200)
        self.assertIn(b'id="speciesSelector"', gallery.data)
        self.assertIn(b'Alpha (current)', gallery.data)
        self.assertIn(b'onchange="queueSelectedSpecies()"', gallery.data)
        self.assertIn(b'class="preview-row"', gallery.data)
        self.assertIn(b'class="preview-stage"', gallery.data)
        self.assertIn(b"previewImg.naturalWidth * 2", gallery.data)
        self.assertIn(b"previewImg.naturalHeight * 2", gallery.data)
        self.assertIn(b"change and queue taxon", gallery.data)
        self.assertNotIn(b">Assign</button>", gallery.data)
        self.assertNotIn(b"Assign &amp; next", gallery.data)
        self.assertIn(b'case "ArrowUp"', gallery.data)
        self.assertIn(b'case "ArrowRight"', gallery.data)
        self.assertIn(f"/pages/classification/{source_id}/Beta".encode(), gallery.data)

        approval = self.client.post(
            f"/review-species/{source_id}/Alpha",
            data={"review_action": "next"},
        )
        self.assertEqual(approval.status_code, 302)
        self.assertTrue(approval.location.endswith(f"/pages/classification/{source_id}/Beta"))

        correction = self.client.post(
            f"/save-species?image_id={source_id}&species=Beta&next=1",
            json=[{"id": beta_id, "species": "Alpha"}],
        )
        self.assertEqual(correction.status_code, 200)
        self.assertEqual(
            correction.get_json()["next_url"],
            f"/pages/classification/{source_id}/Delta",
        )

        with app.app_context():
            self.assertEqual(db.session.get(Samples, alpha_id).review_status, "reviewed")
            corrected = db.session.get(Samples, beta_id)
            self.assertEqual(corrected.species, "Alpha")
            self.assertEqual(corrected.review_status, "reviewed")
            self.assertEqual(corrected.reviews[-1].status, "corrected")

    def test_training_export_filters_balances_splits_and_augments(self):
        image_buffer = io.BytesIO()
        Image.new("L", (16, 16), color=128).save(image_buffer, format="PNG")
        encoded_image = base64.b64encode(image_buffer.getvalue()).decode("ascii")

        with app.app_context():
            user = User(username="exporter", email="exporter@example.com")
            lake = Lakes(lake="Export Lake", country="CH", depth=1, area=1)
            db.session.add_all([user, lake])
            db.session.flush()
            sources = [
                Sampling_metadata(
                    user_id=user.id,
                    lake_id=lake.id,
                    image_name=f"source-{index}.jpg",
                    sampling_date=date(2026, 9, 9),
                    image_path=f"source-{index}.jpg",
                )
                for index in range(2)
            ]
            db.session.add_all(sources)
            db.session.flush()
            samples = [
                Samples(
                    sampling_metadata_id=sources[index % 2].id,
                    x=index * 10,
                    y=0,
                    w=10,
                    h=10,
                    species=species,
                    confidence=0.8,
                    image_base64=encoded_image,
                    review_status=review_status,
                )
                for index, (species, review_status) in enumerate(
                    [
                        ("Species A", "reviewed"),
                        ("Species A", "reviewed"),
                        ("Species A", "unreviewed"),
                        ("Species B", "reviewed"),
                        ("Species B", "reviewed"),
                        ("Species B", "reviewed"),
                    ]
                )
            ]
            db.session.add_all(samples)
            db.session.commit()

            archive = build_species_zip(
                {
                    "review_scope": "reviewed",
                    "species": ["Species A", "Species B"],
                    "distribution": "balanced",
                    "target_per_species": 3,
                    "validation_percent": 20,
                    "seed": 7,
                    "augmentations": ["rotate"],
                    "augmentation_copies": 2,
                }
            )

        with zipfile.ZipFile(archive) as exported:
            manifest = json.loads(exported.read("manifest.json"))
            image_paths = [
                name for name in exported.namelist() if name != "manifest.json"
            ]
            self.assertEqual(len(image_paths), 6)
            self.assertEqual(manifest["options"]["augmentation_copies"], 0)
            self.assertEqual(manifest["species"]["Species A"]["augmented_train"], 1)
            self.assertEqual(manifest["species"]["Species B"]["augmented_train"], 0)
            self.assertTrue(all(path.startswith(("train/", "valid/")) for path in image_paths))
            self.assertTrue(
                all(item["review_status"] == "reviewed" for item in manifest["files"])
            )
            for species in ("Species A", "Species B"):
                training_sources = {
                    item["sampling_metadata_id"]
                    for item in manifest["files"]
                    if item["species"] == species
                    and item["path"].startswith("train/")
                    and not item["augmented"]
                }
                validation_sources = {
                    item["sampling_metadata_id"]
                    for item in manifest["files"]
                    if item["species"] == species
                    and item["path"].startswith("valid/")
                }
                self.assertTrue(training_sources.isdisjoint(validation_sources))


if __name__ == "__main__":
    unittest.main()
