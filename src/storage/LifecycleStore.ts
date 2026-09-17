import { deleteRecord, getAllByIndex, putRecord, STORE_NAMES } from './db'
import type { LifecycleEventRecord } from './types'

export const LifecycleStore = {
  listBySessionId(sessionId: string) {
    return getAllByIndex<LifecycleEventRecord>(STORE_NAMES.lifecycleEvents, 'sessionId', sessionId).then((events) => (
      events.sort((a, b) => a.wallClockMs - b.wallClockMs)
    ))
  },
  put(event: LifecycleEventRecord) {
    return putRecord(STORE_NAMES.lifecycleEvents, event)
  },
  delete(id: string) {
    return deleteRecord(STORE_NAMES.lifecycleEvents, id)
  },
}
