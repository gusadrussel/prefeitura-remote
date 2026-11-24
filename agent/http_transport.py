"""Agent HTTP transport: credentials are never forwarded through redirects."""
from threading import local
import urllib.request

STATE=local()


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_agent_request(request,timeout=20):
    opener=getattr(STATE,'opener',None)
    if opener is None:
        opener=urllib.request.build_opener(RejectRedirect())
        STATE.opener=opener
    return opener.open(request,timeout=timeout)
