import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from fhir_splitter_no_children_agg.database import save_grouped_resources
from fhir_splitter_no_children_agg.splitter import group_by_resource_type, split_parent_children


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.url = f"sqlite:///{Path(self.directory.name) / 'test.db'}"
        self.env = patch.dict(os.environ, {"DATABASE_URL": self.url})
        self.env.start()
        self.addCleanup(self.env.stop)

    def load(self, *resources):
        return save_grouped_resources(group_by_resource_type(list(resources)))

    def query(self, statement):
        engine = create_engine(self.url)
        try:
            with engine.connect() as connection:
                return connection.execute(text(statement)).all()
        finally:
            engine.dispose()

    def test_nested_arrays_have_rows_and_parent_links(self):
        resource = {
            "resourceType": "Patient",
            "id": "p1",
            "name": [{"given": ["Taylor"], "prefix": ["Dr", "Prof"], "suffix": []}],
            "contact": [{"details": {"given": ["Alex"], "tags": [{"value": "a"}]}}],
        }
        split = split_parent_children(resource)
        name = split["children"]["name"][0]
        contact = split["children"]["contact"][0]
        self.assertEqual(name["name_given"], "Taylor")
        self.assertNotIn("name_given", split["children"])
        self.assertEqual([r["name_prefix"] for r in split["children"]["name_prefix"]], ["Dr", "Prof"])
        self.assertEqual(split["children"]["name_suffix"], [])
        self.assertEqual(contact["contact_details_given"], "Alex")
        self.assertNotIn("contact_details_given", split["children"])
        tags = split["children"]["contact_details_tags"][0]
        self.assertEqual(tags["contact_details_tags_value"], "a")
        self.assertEqual(tags["__parent_node_id"], contact["__node_id"])
        self.assertEqual(json.loads(split["parent"][0]["__snapshot"]), resource)

    def test_generated_version_flows_to_children_and_history_is_retained(self):
        def patient(given):
            return {"resourceType": "Patient", "id": "p1", "name": [{"given": given}]}

        self.load(patient(["Taylor"]))
        self.assertEqual(self.load(patient(["Taylor"]))["rows_inserted"], 0)
        self.load(patient(["Taylor", "Lee"]))
        self.assertEqual(self.load(patient(["Taylor", "Lee"]))["rows_inserted"], 0)
        self.assertEqual(
            self.query("SELECT meta_versionid, record_ind FROM fhir_Patient_parent ORDER BY _row_id"),
            [("1", "N"), ("2", "Y")],
        )
        self.assertEqual(
            self.query("SELECT meta_versionid, name_given FROM fhir_Patient_Name_Given ORDER BY _row_id"),
            [("2", "Taylor"), ("2", "Lee")],
        )
        self.assertEqual(
            self.query("SELECT meta_versionid, name_given FROM fhir_Patient_Name ORDER BY _row_id"),
            [("1", "Taylor"), ("2", None)],
        )

    def test_supplied_version_and_duplicate_children(self):
        patient = {
            "resourceType": "Patient",
            "id": "p1",
            "meta": {"versionId": "external-7"},
            "name": [{"given": ["Taylor"]}],
        }
        self.load(patient)
        self.load(patient)
        self.assertEqual(
            self.query("SELECT meta_versionid, name_given FROM fhir_Patient_Name"),
            [("external-7", "Taylor")],
        )

    def test_different_versions_of_same_id_fail_instead_of_mislabeling_children(self):
        first = {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Taylor"]}]}
        second = {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Lee"]}]}
        with self.assertRaisesRegex(RuntimeError, "Load each version separately"):
            self.load(first, second)

    def test_mixed_single_and_multiple_values_preserve_data(self):
        single = {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Taylor"]}]}
        multiple = {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Alex", "Lee"]}]}
        self.load(single, multiple)
        self.assertEqual(self.load(single, multiple)["rows_inserted"], 0)
        rows = self.query("SELECT id, name_given FROM fhir_Patient_Name ORDER BY id")
        self.assertEqual(rows, [("p1", "Taylor"), ("p2", None)])
        rows = self.query("SELECT id, name_given FROM fhir_Patient_Name_Given ORDER BY id, __array_index")
        self.assertEqual(rows, [("p2", "Alex"), ("p2", "Lee")])

    def test_clinical_coding_and_no_structured_columns(self):
        condition = {
            "resourceType": "Condition", "id": "c1",
            "clinicalStatus": {"coding": [
                {"code": "active", "system": "system"},
                {"code": "resolved", "system": "system"},
            ]},
        }
        self.load(condition)
        self.assertEqual(self.load(condition)["rows_inserted"], 0)
        rows = self.query(
            "SELECT meta_versionid, clinicalStatus_coding_code, clinicalStatus_coding_system "
            "FROM fhir_Condition_ClinicalStatus_Coding ORDER BY __array_index"
        )
        self.assertEqual(rows, [("1", "active", "system"), ("1", "resolved", "system")])
        for table in ("fhir_Condition_parent", "fhir_Condition_ClinicalStatus_Coding"):
            columns = self.query(f"PRAGMA table_info({table})")
            self.assertTrue(all(c[2].upper() != "JSON" or c[1] == "__snapshot" for c in columns))

    def test_resources_without_ids_keep_distinct_links(self):
        resource = {"resourceType": "Patient", "name": [{"given": ["Taylor"]}]}
        self.load(resource, resource)
        parents = self.query("SELECT __resource_key FROM fhir_Patient_parent ORDER BY _row_id")
        children = self.query("SELECT __resource_key FROM fhir_Patient_Name ORDER BY _row_id")
        self.assertEqual(len(set(parents)), 2)
        self.assertEqual(parents, children)

    def test_repeated_values_keep_array_positions(self):
        resource = {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Taylor", "Taylor"]}]}
        self.load(resource)
        self.assertEqual(self.load(resource)["rows_inserted"], 0)
        self.assertEqual(
            self.query("SELECT name_given, __array_index FROM fhir_Patient_Name_Given ORDER BY __array_index"),
            [("Taylor", 0), ("Taylor", 1)],
        )

    def test_single_string_at_root_and_nested_object_arrays(self):
        resource = {
            "resourceType": "Observation",
            "id": "o1",
            "alias": ["Taylor"],
            "details": {"note": ["One"]},
            "components": [{"label": ["First"]}],
            "coding": [{"code": "active", "system": "system"}],
        }
        split = split_parent_children(resource)
        self.assertEqual(split["parent"][0]["alias"], "Taylor")
        self.assertEqual(split["parent"][0]["details_note"], "One")
        self.assertEqual(split["children"]["components"][0]["components_label"], "First")
        self.assertEqual(split["children"]["coding"][0]["coding_code"], "active")
        self.assertNotIn("alias", split["children"])
        self.assertNotIn("details_note", split["children"])
        self.assertNotIn("components_label", split["children"])
        self.load(resource)
        self.assertEqual(self.load(resource)["rows_inserted"], 0)
        self.assertEqual(self.query("SELECT alias, details_note FROM fhir_Observation_parent"), [("Taylor", "One")])
        self.assertEqual(self.query("SELECT components_label FROM fhir_Observation_Components"), [("First",)])

    def test_reference_ids_in_parent_children_and_snapshot(self):
        resource = {
            "resourceType": "Observation",
            "id": "o1",
            "subject": {"reference": "Patient/p1"},
            "encounter": {"reference": "https://example.test/fhir/Encounter/e1/_history/2"},
            "performer": [
                {"reference": "Practitioner/pr1", "display": "Practitioner/pr1"},
                {"reference": "urn:uuid:abc-def"},
                {"reference": "#contained-1"},
            ],
        }
        split = split_parent_children(resource)
        parent = split["parent"][0]
        self.assertEqual(parent["subject_reference"], "p1")
        self.assertEqual(parent["encounter_reference"], "e1")
        self.assertEqual(json.loads(parent["__snapshot"]), resource)
        rows = split["children"]["performer"]
        self.assertEqual([row["performer_reference"] for row in rows], ["pr1", "abc-def", "contained-1"])
        self.assertEqual(rows[0]["performer_display"], "Practitioner/pr1")
        self.load(resource)
        self.assertEqual(self.load(resource)["rows_inserted"], 0)
        self.assertEqual(
            self.query("SELECT subject_reference, encounter_reference FROM fhir_Observation_parent"),
            [("p1", "e1")],
        )
        self.assertEqual(
            self.query("SELECT performer_reference FROM fhir_Observation_Performer ORDER BY __array_index"),
            [("pr1",), ("abc-def",), ("contained-1",)],
        )

    def test_sample_bundle_encounter_reference_matches_encounter_id(self):
        from fhir_splitter_no_children_agg.splitter import collect_resources_except_bundle

        bundle_path = Path(__file__).parent / "195352.bundle.json"
        if not bundle_path.exists():
            self.skipTest("Local example bundle is not present")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        resources = collect_resources_except_bundle(bundle)
        observations = [r for r in resources if r.get("resourceType") == "Observation" and r.get("encounter")]
        encounters = {r["id"] for r in resources if r.get("resourceType") == "Encounter" and r.get("id")}
        self.assertTrue(observations)
        for observation in observations:
            row = split_parent_children(observation)["parent"][0]
            self.assertIn(row["encounter_reference"], encounters)


if __name__ == "__main__":
    unittest.main()
