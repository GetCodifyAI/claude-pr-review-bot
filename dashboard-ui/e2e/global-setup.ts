// Ensure the fixture + auth cookie exist before the workers start (the webServer command also
// builds them, but Playwright's setup/webServer order isn't guaranteed across versions, so we
// build here too — idempotent, and the fixed secret keeps everything in agreement).
import { buildFixture, mintAuthState } from "./fixture";

export default function globalSetup() {
  buildFixture();
  mintAuthState();
}
