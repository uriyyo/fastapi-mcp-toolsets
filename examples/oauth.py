# Your own sign-in turned into an OAuth authorization server for MCP clients: the client is sent to this app's
# login page, the page posts who signed in back to the callback, and the server issues its own tokens.
# Tokens are checked by a FastAPI security dependency, tools declare the scopes they need, deps get the token.
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI, Security
from fastapi.responses import HTMLResponse
from fastapi_injected import DepFactory
from mcp.shared.auth import OAuthClientInformationFull
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPAdapter, MCPDeps, MCPRouter
from fastapi_mcp_toolsets.auth import GuardedTools, requires_scopes
from fastapi_mcp_toolsets.oauth import (
    AccessToken,
    AuthorizationServer,
    ClientRegistrationOptions,
    Identity,
    OAuthBearer,
    ProtectedResource,
    RevocationOptions,
    StoredProvider,
    Transaction,
)
from fastapi_mcp_toolsets.oauth.backends import MemoryStorage
from fastapi_mcp_toolsets.oauth.verifiers import JWTVerifier

BASE_URL: Final = "http://localhost:8000"
SCOPES: Final = ["notes:read", "notes:write"]

# Who may sign in, and with what; a real app checks a password, a session cookie or an ID token here.
USERS: Final = {"anne": "anne-secret", "bob": "bob-secret"}  # noqa: S105


async def authenticate(payload: dict[str, Any], transaction: Transaction) -> Identity | None:
    match payload:
        case {"username": str() as username, "password": str() as password} if USERS.get(username) == password:
            return Identity(subject=username, claims={"name": username.title()})
        case _:
            return None


# With Firebase instead, the page signs in with the Firebase SDK and posts the ID token; the same shape
# works for any identity provider whose tokens a `TokenVerifier` can check.
FIREBASE_PROJECT: Final = os.environ.get("FIREBASE_PROJECT")


async def authenticate_firebase(payload: dict[str, Any], transaction: Transaction) -> Identity | None:
    verifier = JWTVerifier(
        jwks_url="https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com",
        issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT}",
        audience=FIREBASE_PROJECT,
    )

    if (token := await verifier.verify_token(str(payload.get("id_token", "")))) is None:
        return None

    return Identity(subject=token.subject or token.client_id, claims=token.claims or {})


provider = StoredProvider(storage=MemoryStorage(), issuer_url=BASE_URL, login_url=f"{BASE_URL}/login")
# The sign-in page posts back to the provider; this turns the post into an identity.
provider.authenticate(authenticate_firebase if FIREBASE_PROJECT else authenticate)


@provider.on.identity
async def known_users_only(
    identity: Identity, client: OAuthClientInformationFull, transaction: Transaction
) -> Identity:
    # Runs after sign-in, before a code is issued: the place to enrich claims or refuse an account.
    return identity.model_copy(update={"claims": {**identity.claims, "client": client.client_name}})


auth_server = AuthorizationServer.stored(
    provider,
    client_registration=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
    revocation=RevocationOptions(enabled=True),
)

resource = ProtectedResource(
    resource_url=f"{BASE_URL}/mcp",
    authorization_servers=[BASE_URL],
    scopes_supported=SCOPES,
    resource_name="Notes",
)

# Every request needs a valid token; `flows` lets Swagger UI (`/docs`) obtain one.
bearer = OAuthBearer(verifier=auth_server.verifier, resource=resource, flows=auth_server.flows())


@dataclass
class Deps(MCPDeps):
    # The token the route already verified, from the request's dependency cache.
    token: DepFactory[AccessToken, bearer]

    @property
    def user(self) -> str:
        return self.token.subject or self.token.client_id


# Tools the token lacks the scopes for are left out of `tools/list` and refused on `tools/call`.
adapter = MCPAdapter(deps_type=Deps, name="notes", capabilities=[GuardedTools(bearer)])

NOTES: dict[str, dict[str, str]] = defaultdict(dict)


@adapter.tool(metadata=requires_scopes("notes:read"))
async def list_notes(ctx: RunContext[Deps]) -> list[str]:
    """List the titles of the user's notes."""
    return sorted(NOTES[ctx.deps.user])


@adapter.tool(metadata=requires_scopes("notes:write"))
async def save_note(ctx: RunContext[Deps], title: str, text: str) -> str:
    """Save a note."""
    NOTES[ctx.deps.user][title] = text
    return f"Saved {title!r} for {ctx.deps.user}"


@adapter.tool
async def whoami(ctx: RunContext[Deps]) -> str:
    """Who the token belongs to, and what it may do."""
    return f"{ctx.deps.user} ({ctx.deps.token.client_id}) with scopes {' '.join(ctx.deps.token.scopes)}"


app = FastAPI()
# The endpoint, the authorization server's routes and the resource metadata, all from one router.
app.include_router(MCPRouter(adapter, dependencies=[Security(bearer)], extensions=[auth_server, resource]))


LOGIN_PAGE: Final = """
<!doctype html>
<title>Sign in</title>
<h1>Sign in to Notes</h1>
<p id="client"></p>
<form id="login">
  <input name="username" placeholder="username" required>
  <input name="password" type="password" placeholder="password" required>
  <button>Sign in</button>
  <button type="button" id="deny">Deny</button>
</form>
<script>
  const state = new URLSearchParams(location.search).get("state");
  fetch(`/oauth/transaction?state=${state}`).then(r => r.json()).then(client => {
    document.getElementById("client").textContent =
      `${client.client_name ?? client.client_id} asks for: ${(client.scopes ?? []).join(", ")}`;
  });
  async function finish(body) {
    const response = await fetch("/oauth/callback", {
      method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify({state, ...body}),
    });
    const answer = await response.json();
    if (answer.redirect_url) location.href = answer.redirect_url; else alert(answer.error_description);
  }
  document.getElementById("login").onsubmit = (event) => {
    event.preventDefault();
    finish(Object.fromEntries(new FormData(event.target)));
  };
  document.getElementById("deny").onclick = () => finish({deny: true});
</script>
"""


@app.get("/login", include_in_schema=False)
async def login() -> HTMLResponse:
    return HTMLResponse(LOGIN_PAGE)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
