<script setup>
import { authState } from "../api/auth";

/* Machine access, as a read-only readout.
 *
 * The API key is no longer a browser credential - humans sign in, and this panel
 * only reports what camera nodes and scripts are up against. It is deliberately
 * not editable here: the key lives in the server's environment, and a UI that
 * appeared to change it would either be lying or would be a route to locking
 * every node out from a browser session.
 */
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <h2>
        Node access
        <span v-if="authState.apiKeyRequired" class="badge success">key required</span>
        <span v-else class="badge warning">open</span>
      </h2>
    </div>

    <div class="panel-body">
      <p class="field-hint">
        Camera nodes and automation authenticate with the shared API key, not with your
        login — a node cannot answer a two-factor prompt. The key is set on the server as
        <code>SKYHUB_SERVER_API_KEY</code> and never reaches this browser.
      </p>

      <dl class="data-list">
        <div class="data-row">
          <dt>Your session</dt>
          <dd class="data-value">Username, password and a two-factor code</dd>
        </div>
        <div class="data-row">
          <dt>Nodes and scripts</dt>
          <dd class="data-value">
            <template v-if="authState.apiKeyRequired">API key required</template>
            <template v-else>No key configured</template>
          </dd>
        </div>
      </dl>

      <p v-if="!authState.apiKeyRequired" class="callout warning">
        No API key is set, so anything that can reach this port can upload captures and
        connect as a node. Your own login is unaffected. Set
        <code>SKYHUB_SERVER_API_KEY</code> and restart the server, then give every node
        the same value as <code>SKYHUB_NODE_API_KEY</code>.
      </p>

      <p class="field-hint">
        The API key cannot change your password, replace your authenticator or read your
        sessions — those need a signed-in browser. See <code>docs/AUTH.md</code> for the
        full split, including how to restrict the key to node routes only.
      </p>
    </div>
  </section>
</template>
