export function shouldShowPlaybackControls(isPreparing: boolean, sourceUrl: string | null | undefined): boolean {
  return Boolean(sourceUrl) && !isPreparing
}
