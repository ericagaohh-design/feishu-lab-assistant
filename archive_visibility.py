"""Verify both public-access views of a newly created Wiki document."""


class VisibilityError(RuntimeError):
    pass


def _settings(wiki, kind, token):
    result = wiki.request("GET", f"/open-apis/drive/v2/permissions/{token}/public",
                          query={"type": kind})
    values = result.get("permission_public") or {}
    link = values.get("link_share_entity")
    external = values.get("external_access_entity")
    if not link or not external:
        raise VisibilityError("飞书未返回完整的链接分享和对外分享设置。")
    return link, external


def restrict_and_verify(wiki, node_token, doc_token, *, restrict=True):
    """Call only on a verified original node. Pass restrict=False for read-only checks."""
    closed = ("closed", "closed")
    node_before = _settings(wiki, "wiki", node_token)
    doc_before = _settings(wiki, "docx", doc_token)
    if node_before == doc_before == closed:
        return
    if restrict:
        # The Wiki link setting belongs to the node. External access for a Wiki
        # document can be restricted using the underlying Docx on the v1 API.
        if node_before[0] != "closed":
            wiki.request("PATCH", f"/open-apis/drive/v1/permissions/{node_token}/public",
                         query={"type": "wiki"}, body={"link_share_entity": "closed"})
        changes = {}
        if doc_before[0] != "closed":
            changes["link_share_entity"] = "closed"
        if node_before[1] != "closed" or doc_before[1] != "closed":
            changes["external_access"] = False
        if changes:
            wiki.request("PATCH", f"/open-apis/drive/v1/permissions/{doc_token}/public",
                         query={"type": "docx"}, body=changes)
        node_after = _settings(wiki, "wiki", node_token)
        doc_after = _settings(wiki, "docx", doc_token)
        if node_after == doc_after == closed:
            return
    raise VisibilityError("飞书尚未确认知识库页面和底层文档均已关闭链接分享与对外分享。")
