import React from "react";
import MobileArticleReview, {
  type MobileReaderProps,
} from "./MobileArticleReview";

// Cross-origin pages cannot be read from the web app. Offer the same explicit,
// native-rendered paste flow instead of claiming an iframe can import them.
export default function LocalBrowser(props: MobileReaderProps) {
  return <MobileArticleReview {...props} />;
}
