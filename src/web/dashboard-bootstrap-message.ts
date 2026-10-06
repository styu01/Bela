/**
 * Render the one-time dashboard bootstrap notice without accidentally placing
 * the bearer credential into a redirected stderr stream.
 */
export function dashboardBootstrapMessage(port: number, dashboardToken: string, stderrIsTTY: boolean | undefined): string {
  if (stderrIsTTY) {
    return `\nDashboard access URL (paste into browser, token is stored afterward):\n  http://127.0.0.1:${port}/?token=${dashboardToken}\n\n`
  }
  return `\nDashboard: http://127.0.0.1:${port}/ -- access token in store/.dashboard-token (not printed to a redirected stream)\n\n`
}
