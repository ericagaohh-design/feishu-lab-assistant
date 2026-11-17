import unittest
from unittest.mock import Mock, call

from archive_visibility import VisibilityError, restrict_and_verify


def settings(link="closed", external="closed"):
    return {"permission_public": {"link_share_entity": link, "external_access_entity": external}}


class TestArchiveVisibility(unittest.TestCase):
    def test_existing_restricted_document_is_read_only(self):
        wiki = Mock()
        wiki.request.side_effect = [settings(), settings()]
        restrict_and_verify(wiki, "node", "doc")
        self.assertEqual(wiki.request.call_args_list, [
            call("GET", "/open-apis/drive/v2/permissions/node/public", query={"type": "wiki"}),
            call("GET", "/open-apis/drive/v2/permissions/doc/public", query={"type": "docx"}),
        ])

    def test_restrict_only_requested_fields_and_verify_node_and_document(self):
        wiki = Mock()
        wiki.request.side_effect = [
            settings("tenant_readable", "open"), settings("tenant_readable", "open"),
            {}, {}, settings(), settings(),
        ]
        restrict_and_verify(wiki, "node", "doc")
        calls = wiki.request.call_args_list
        self.assertEqual(calls[2:4], [
            call("PATCH", "/open-apis/drive/v1/permissions/node/public",
                 query={"type": "wiki"}, body={"link_share_entity": "closed"}),
            call("PATCH", "/open-apis/drive/v1/permissions/doc/public",
                 query={"type": "docx"}, body={"link_share_entity": "closed", "external_access": False}),
        ])
        self.assertEqual(calls[:2], calls[-2:])

    def test_denied_or_ineffective_update_preserves_unverified_state(self):
        wiki = Mock()
        wiki.request.side_effect = [settings(), settings(external="open"), {}, settings(), settings(external="open")]
        with self.assertRaises(VisibilityError):
            restrict_and_verify(wiki, "node", "doc")
        self.assertEqual(wiki.request.call_args_list[2].kwargs["body"], {"external_access": False})
        wiki.request.side_effect = [settings(), settings("tenant_editable"), RuntimeError("denied")]
        with self.assertRaisesRegex(RuntimeError, "denied"):
            restrict_and_verify(wiki, "node", "doc")

    def test_read_only_or_incomplete_settings_stop_before_modification(self):
        for responses in ([settings("anyone_readable"), settings()], [settings(), {"permission_public": {}}]):
            wiki = Mock()
            wiki.request.side_effect = responses
            with self.assertRaises(VisibilityError):
                restrict_and_verify(wiki, "node", "doc", restrict=False)
            self.assertTrue(all(args.args[0] == "GET" for args in wiki.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
