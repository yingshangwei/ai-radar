import type { Connection } from "./types";

export default function DeviceReading(_: {
  connection: Connection;
  paused: boolean;
  onStatus: (message: string) => void;
  onComplete: () => void;
}) {
  return null;
}
