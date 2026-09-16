export type AudioProfileId = 'browser-default' | 'speech' | 'raw-ish'

export interface AudioProfile {
  id: AudioProfileId
  name: string
  shortName: string
  description: string
  constraints: MediaStreamConstraints
}

export const AUDIO_PROFILES: AudioProfile[] = [
  {
    id: 'browser-default',
    name: 'Browser Default',
    shortName: 'Default',
    description: '只请求 audio: true，由浏览器自行决定处理链。',
    constraints: { audio: true },
  },
  {
    id: 'speech',
    name: 'Speech',
    shortName: 'Speech',
    description: '尝试单声道、降噪和自动增益，关闭回声消除。',
    constraints: {
      audio: {
        channelCount: { ideal: 1 },
        noiseSuppression: { ideal: true },
        autoGainControl: { ideal: true },
        echoCancellation: { ideal: false },
      },
    },
  },
  {
    id: 'raw-ish',
    name: 'Raw-ish',
    shortName: 'Raw-ish',
    description: '尝试关闭浏览器音频处理，便于比较原始听感。',
    constraints: {
      audio: {
        channelCount: { ideal: 1 },
        noiseSuppression: { ideal: false },
        autoGainControl: { ideal: false },
        echoCancellation: { ideal: false },
      },
    },
  },
]

export function getAudioProfile(id: AudioProfileId): AudioProfile {
  return AUDIO_PROFILES.find((profile) => profile.id === id) ?? AUDIO_PROFILES[0]
}
