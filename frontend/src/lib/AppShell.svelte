<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { Identity } from './types';
  let { identity, children }: { identity: Identity | null; children: Snippet } = $props();
</script>

<div class="app-shell">
  <header class="site-header">
    <a class="brand" href="/mod-web"
      ><span class="brand-symbol" aria-hidden="true">Y</span> Yukibot
      <span class="brand-section">/ Network</span></a
    >
    <nav aria-label="Account">
      <a href="/mod-web">Dashboard</a>
      {#if identity}<span class="account-name">{identity.display_name}</span><a href="/auth/logout"
          >Sign out</a
        >{/if}
    </nav>
  </header>
  <main>{@render children()}</main>
  <footer>
    Yukibot <span>Browser → configured endpoints</span>
  </footer>
</div>
