export type TranslationUpdate = {
  id: string;
  article_id: string;
  completed_at: string;
  title: string;
  title_zh: string;
  kind: string;
  author: string;
};
export const unseenUpdates = (items: TranslationUpdate[], seen: string) =>
  seen
    ? items.filter((item) => Date.parse(item.completed_at) > Date.parse(seen))
        .length
    : 0;
