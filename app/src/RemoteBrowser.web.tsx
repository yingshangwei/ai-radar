import React from "react";
export default function RemoteBrowser({
  url,
}: {
  url: string;
  onDone: () => void;
}) {
  return (
    <iframe
      title="网页授权浏览器"
      src={url}
      style={{ border: 0, width: "100%", height: "100%", flex: 1 }}
    />
  );
}
