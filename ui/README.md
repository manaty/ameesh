# Embeddable read screens

The bundle served at `/ui/ameesh-ui.js` registers `<ameesh-app>` and seven
independent elements: `<ameesh-projects>`, `<ameesh-timeline>`, `<ameesh-agent>`,
`<ameesh-alerts>`, `<ameesh-hosts>`, `<ameesh-budget>` and `<ameesh-thread>`.
The standalone `/ui/` page assembles them with internal navigation.

An embedded screen uses the nearest `<ameesh-config api-base="…">`, or a
client supplied by the host:

```js
import { createClient } from './ameesh-ui.js';
const screen = document.querySelector('ameesh-timeline');
screen.client = createClient({ baseUrl: meshUrl, tokenProvider: getCurrentToken });
screen.project = 'ameesh';
```

`project` selects the timeline, project thread and optional plan gauges in the
budget screen. `agent` selects the agent card. `lot` narrows a thread to a lot;
without it the thread is the project's. The projects screen accepts an optional
`project` filter. Without a required selection, the component displays a prompt.
`lang="fr"` / `lang="en"`, `theme` and the L88 CSS tokens apply to each screen.

Selection buttons emit bubbling, composed `ameesh:open-project`,
`ameesh:open-agent` and `ameesh:open-lot` events. Their `detail` contains the
selected `project`, `agent` or `lot`; the host chooses navigation. The app handles
these events internally while leaving them available to its host. Thread text
is rendered literally. These screens do not expose write actions.

Views using the same client share one `/api/v1/events` connection. Event bursts
coalesce into at most one scheduled refresh per second per view; a change during
a read schedules another read afterward. Reconnection and `reset` also reload
the snapshot. Removing the last view aborts the stream. A missing SSE endpoint
keeps the initial snapshot and manual Refresh button available with a visible
status. L85 (#373) is required to validate this against the real event service.

Run `npm run typecheck`, `npm test`, `npm run build`, then `npm run check` in
`ui/`. The build is committed in `src/ameesh/ui/static/` for Python packaging.
