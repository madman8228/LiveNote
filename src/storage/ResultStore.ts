import { getAllRecords, getRecord, putRecord, STORE_NAMES } from './db'
import type { PublishedResultRecord } from './types'

export const ResultStore = {
  get(sessionId: string): Promise<PublishedResultRecord | undefined> {
    return getRecord<PublishedResultRecord>(STORE_NAMES.results, sessionId)
  },
  put(result: PublishedResultRecord): Promise<void> {
    return putRecord(STORE_NAMES.results, result)
  },
  list(): Promise<PublishedResultRecord[]> {
    return getAllRecords<PublishedResultRecord>(STORE_NAMES.results)
  },
}
