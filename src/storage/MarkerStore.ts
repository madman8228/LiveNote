import { deleteRecord, getAllByIndex, getAllRecords, putRecord, STORE_NAMES } from './db'
import type { MarkerRecord } from './types'

export const MarkerStore = {
  listBySessionId(sessionId: string) {
    return getAllByIndex<MarkerRecord>(STORE_NAMES.markers, 'sessionId', sessionId).then((markers) => (
      markers.sort((a, b) => a.elapsedMs - b.elapsedMs || a.createdAt - b.createdAt)
    ))
  },
  put(marker: MarkerRecord) {
    return putRecord(STORE_NAMES.markers, marker)
  },
  delete(id: string) {
    return deleteRecord(STORE_NAMES.markers, id)
  },
  listAll() {
    return getAllRecords<MarkerRecord>(STORE_NAMES.markers)
  },
  listPendingOrFailed() {
    return getAllRecords<MarkerRecord>(STORE_NAMES.markers).then((markers) => markers
      .filter((marker) => marker.uploadStatus === 'PENDING' || marker.uploadStatus === 'FAILED')
      .sort((a, b) => a.createdAt - b.createdAt))
  },
  listUploading() {
    return getAllRecords<MarkerRecord>(STORE_NAMES.markers).then((markers) => (
      markers.filter((marker) => marker.uploadStatus === 'UPLOADING')
    ))
  },
}
