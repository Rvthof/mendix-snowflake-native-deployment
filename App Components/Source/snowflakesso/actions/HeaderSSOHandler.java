package snowflakesso.actions;

import com.mendix.core.Core;
import com.mendix.core.CoreException;
import com.mendix.externalinterface.connector.RequestHandler;
import com.mendix.m2ee.api.IMxRuntimeRequest;
import com.mendix.m2ee.api.IMxRuntimeResponse;
import com.mendix.systemwideinterfaces.core.IContext;
import com.mendix.systemwideinterfaces.core.IMendixObject;
import com.mendix.systemwideinterfaces.core.ISession;
import com.mendix.systemwideinterfaces.core.IUser;

import com.mendix.core.objectmanagement.member.MendixObjectReferenceSet;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

public class HeaderSSOHandler extends RequestHandler {

    private static final String HEADER_SF_USER = "Sf-Context-Current-User";
    private static final String HEADER_SF_TOKEN = "Sf-Context-Current-User-Token";
    private static final String LOG_NODE = "SnowflakeSSO";

    // Configurable: the Mendix user role to assign to auto-provisioned users.
    // Set this via a Mendix constant or hardcode for POC.
    private static final String DEFAULT_USER_ROLE = "User";

    @Override
    protected void processRequest(
            IMxRuntimeRequest request,
            IMxRuntimeResponse response,
            String path) throws Exception {

        HttpServletRequest httpReq = request.getHttpServletRequest();
        HttpServletResponse httpResp = response.getHttpServletResponse();

        // Handle token refresh endpoint (lightweight, no session creation)
        if (path != null && path.startsWith("refresh")) {
            handleTokenRefresh(request, response, httpReq);
            return;
        }

        // 1. Read the trusted identity header
        String snowflakeUsername = httpReq.getHeader(HEADER_SF_USER);

        if (snowflakeUsername == null || snowflakeUsername.isBlank()) {
            Core.getLogger(LOG_NODE).error(
                "No " + HEADER_SF_USER + " header found. "
                + "This handler must only be accessed through the SPCS ingress proxy.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            httpResp.setContentType("text/plain");
            httpResp.getWriter().write("Authentication required.");
            return;
        }

        Core.getLogger(LOG_NODE).info("SSO login for Snowflake user: " + snowflakeUsername);

        IContext systemContext = Core.createSystemContext();

        // 2. Find or create the Mendix user as SnowflakeUser
        IUser user = Core.getUser(systemContext, snowflakeUsername);

        if (user == null) {
            user = provisionUser(systemContext, snowflakeUsername);
            Core.getLogger(LOG_NODE).info("Auto-provisioned new user: " + snowflakeUsername);
        } else {
            // Check if the existing user is a SnowflakeUser specialization.
            // If not (e.g. created as plain System.User before), delete and re-create.
            IMendixObject existingObj = user.getMendixObject();
            if (!existingObj.isInstanceOf("SnowflakeSSO.SnowflakeUser")) {
                Core.getLogger(LOG_NODE).warn(
                    "User " + snowflakeUsername + " exists as " + existingObj.getType()
                    + ", upgrading to SnowflakeSSO.SnowflakeUser");
                Core.delete(systemContext, existingObj);
                user = provisionUser(systemContext, snowflakeUsername);
            }
        }

        // 3. Initialize a session (bypasses password check)
        ISession session = Core.initializeSession(user, null);

        if (session == null) {
            Core.getLogger(LOG_NODE).error("Failed to initialize session for: " + snowflakeUsername);
            response.setStatus(IMxRuntimeResponse.INTERNAL_SERVER_ERROR);
            httpResp.getWriter().write("Session initialization failed.");
            return;
        }

        // 4. Store the caller token on the user object
        String callerToken = httpReq.getHeader(HEADER_SF_TOKEN);
        if (callerToken != null && !callerToken.isBlank()) {
            IMendixObject userObj = user.getMendixObject();
            userObj.setValue(systemContext, "CallerToken", callerToken);
            Core.commit(systemContext, userObj);
            Core.getLogger(LOG_NODE).debug("Stored caller token for: " + snowflakeUsername);
        }

        // 5. Let Mendix set all session cookies (XASSESSIONID, CSRF token, flags)
        Core.addMendixCookies(request, response, session, false);

        // 6. Redirect to the original destination or home
        String cont = httpReq.getParameter("cont");
        String redirectUrl = "/index.html";
        if (cont != null && !cont.isBlank()) {
            redirectUrl = URLDecoder.decode(cont, StandardCharsets.UTF_8);
            // Safety: only allow relative redirects
            if (redirectUrl.startsWith("http") || redirectUrl.startsWith("//")) {
                redirectUrl = "/index.html";
            }
        }

        response.setStatus(IMxRuntimeResponse.SEE_OTHER);
        response.addHeader("Location", redirectUrl);

        Core.getLogger(LOG_NODE).debug(
            "Session created for " + snowflakeUsername + ", redirecting to " + redirectUrl);
    }

    /**
     * Lightweight endpoint for refreshing the caller token.
     * Called periodically by the client JS action via /headersso/refresh.
     * Resolves the user from the existing Mendix session cookie.
     */
    private void handleTokenRefresh(
            IMxRuntimeRequest request,
            IMxRuntimeResponse response,
            HttpServletRequest httpReq) throws Exception {

        String callerToken = httpReq.getHeader(HEADER_SF_TOKEN);
        if (callerToken == null || callerToken.isBlank()) {
            Core.getLogger(LOG_NODE).warn("Token refresh called but no " + HEADER_SF_TOKEN + " header present.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        ISession session = this.getSessionFromRequest(request);
        if (session == null) {
            Core.getLogger(LOG_NODE).warn("Token refresh called but no active session found.");
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        IContext systemContext = Core.createSystemContext();
        IUser user = session.getUser(systemContext);
        if (user == null) {
            response.setStatus(IMxRuntimeResponse.UNAUTHORIZED);
            return;
        }

        IMendixObject userObj = user.getMendixObject();
        userObj.setValue(systemContext, "CallerToken", callerToken);
        Core.commit(systemContext, userObj);

        Core.getLogger(LOG_NODE).debug("Refreshed caller token for: " + user.getName());
        response.setStatus(204);
    }

    /**
     * Creates a new Mendix user (SnowflakeSSO.SnowflakeUser) for the given Snowflake username.
     * Assigns the configured default role.
     */
    private IUser provisionUser(IContext systemContext, String username) throws CoreException {
        IMendixObject accountObj = Core.instantiate(systemContext, "SnowflakeSSO.SnowflakeUser");
        accountObj.setValue(systemContext, "Name", username);
        // Set a random unusable password (login is header-based only)
        accountObj.setValue(systemContext, "Password", "SSO_" + UUID.randomUUID());

        // Assign user role via reference set add (safe, does not overwrite)
        List<IMendixObject> roles = Core.createXPathQuery(
            String.format("//System.UserRole[Name='%s']", DEFAULT_USER_ROLE))
            .execute(systemContext);

        if (!roles.isEmpty()) {
            MendixObjectReferenceSet userRoles =
                (MendixObjectReferenceSet) accountObj.getMember("System.UserRoles");
            userRoles.addValue(systemContext, roles.get(0).getId());
        }

        Core.commit(systemContext, accountObj);
        return Core.getUser(systemContext, username);
    }
}
