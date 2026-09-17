import MathText from "./MathText";
import React, { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Modal,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { APIError } from "./api";
import { C, s } from "./theme";
import type { Connection } from "./types";
import type {
  IndustryAssessment,
  IndustryClaim,
  IndustryEvidence,
  IndustryJob,
  IndustryReport,
  IndustrySource,
  IndustryTheme,
} from "./industryTypes";
import {
  fetchIndustry,
  fetchIndustryEvidence,
  fetchIndustryEvidenceById,
  fetchIndustryJob,
  industryAccessDenied,
  industryError,
  refreshIndustry,
  setIndustryTracking,
} from "./industryApi";
import {
  assessmentFreshness,
  assessmentStatusLabel,
  evidencePublishedLabel,
  industryJobActive,
  industryJobLabel,
  industryQueryKey,
  industrySourceLabel,
  industryStateLabel,
  industryTime,
  originalWebURL,
  visibleAssessment,
} from "./industryView";

const retryRead = (count: number, error: Error) =>
  !(error instanceof APIError && [401, 403, 404].includes(error.status)) &&
  count < 1;

function Action({
  label,
  onPress,
  disabled = false,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={[i.action, disabled && { opacity: 0.45 }]}
    >
      <Text style={i.actionText}>{label}</Text>
    </Pressable>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <View style={[s.note, { marginVertical: 12 }]}>
      <Text style={s.body}>{children}</Text>
    </View>
  );
}

export default function IndustryPanel({
  connection,
  active = true,
}: {
  connection: Connection;
  active?: boolean;
}) {
  if (connection.demo)
    return (
      <ScrollView contentContainerStyle={i.screen}>
        <Text style={s.h1}>{"产业变化，\n逐步验证。"}</Text>
        <Notice>
          示例模式没有真实行业报告。连接服务器后，可查看已采集证据、行业判断和观察指标。
        </Notice>
      </ScrollView>
    );
  // Remount local selections/mutations when credentials change, as well as isolating query data.
  return (
    <IndustrySession
      key={JSON.stringify(industryQueryKey(connection))}
      connection={connection}
      active={active}
    />
  );
}

function IndustrySession({
  connection,
  active,
}: {
  connection: Connection;
  active: boolean;
}) {
  const client = useQueryClient();
  const key = industryQueryKey(connection);
  const reportKey = [...key, "report"];
  const [selected, setSelected] = useState("");
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [originalId, setOriginalId] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<IndustryJob | null>(
    () => client.getQueryData([...key, "last-submission"]) || null,
  );
  const query = useQuery({
    queryKey: reportKey,
    queryFn: ({ signal }) => fetchIndustry(connection, signal),
    enabled: active,
    refetchInterval: active ? 30_000 : false,
    retry: retryRead,
  });
  const refresh = useMutation({
    mutationFn: () => refreshIndustry(connection),
    retry: false,
    onSuccess: (job) => {
      setSubmitted(job);
      client.setQueryData([...key, "last-submission"], job);
      client.setQueryData([...key, "job", job.id], job);
      void client.invalidateQueries({ queryKey: reportKey });
    },
  });
  const tracking = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setIndustryTracking(connection, id, enabled),
    retry: false,
    onSuccess: (result) => {
      client.setQueryData<IndustryReport>(
        reportKey,
        (old) =>
          old && {
            ...old,
            themes: old.themes.map((theme) =>
              theme.id === result.id
                ? { ...theme, enabled: result.enabled }
                : theme,
            ),
          },
      );
      void client.invalidateQueries({ queryKey: reportKey });
    },
  });
  const accessDenied = [query.error, refresh.error, tracking.error].some(
    industryAccessDenied,
  );
  const unavailable =
    query.error instanceof APIError && query.error.status === 404;
  const data = accessDenied || unavailable ? undefined : query.data;
  const lastJob = industryJobActive(submitted)
    ? submitted
    : data?.latest_job || submitted;
  const jobQuery = useQuery({
    queryKey: [...key, "job", lastJob?.id],
    queryFn: ({ signal }) => fetchIndustryJob(connection, lastJob!.id, signal),
    enabled: active && !!lastJob?.id && !accessDenied && !unavailable,
    initialData: lastJob || undefined,
    staleTime: 0,
    refetchInterval: (q) =>
      active && industryJobActive(q.state.data) ? 3000 : false,
    retry: retryRead,
  });
  const job = jobQuery.data || lastJob;
  useEffect(() => {
    if (job && submitted?.id === job.id && submitted.status !== job.status) {
      setSubmitted(job);
      client.setQueryData([...key, "last-submission"], job);
    }
  }, [
    job?.id,
    job?.status,
    submitted?.id,
    submitted?.status,
    connection.url,
    connection.token,
    client,
  ]);
  useEffect(() => {
    if (!job || industryJobActive(job)) return;
    void client.invalidateQueries({ queryKey: reportKey });
    void client.invalidateQueries({ queryKey: [...key, "evidence"] });
    // Only terminal transitions invalidate content; ordinary status polls do not.
  }, [job?.id, job?.status, connection.url, connection.token, client]);
  const theme =
    data?.themes.find((item) => item.id === selected) || data?.themes[0];
  const updating = refresh.isPending || industryJobActive(job);
  const stale = query.isError;
  const reload = () => {
    refresh.reset();
    tracking.reset();
    void query.refetch();
    if (lastJob?.id) void jobQuery.refetch();
    void client.invalidateQueries({ queryKey: [...key, "evidence"] });
  };

  return (
    <>
      <ScrollView
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={i.screen}
        refreshControl={
          <RefreshControl
            refreshing={query.isRefetching}
            onRefresh={reload}
            tintColor={C.green}
          />
        }
      >
        <Text style={s.h1}>{"产业变化，\n逐步验证。"}</Text>
        <Text style={[s.body, { color: C.muted, marginTop: 8 }]}>
          从 AI 进展到经营兑现，保留支持、反证与未知。
        </Text>
        <View style={[s.spread, { marginTop: 16 }]}>
          <Text style={i.eyebrow}>行业研究 · 免费信息源</Text>
          <Action
            label={
              refresh.isPending
                ? "正在提交…"
                : industryJobActive(job)
                  ? industryJobLabel(job!)
                  : "采集最新证据"
            }
            disabled={!!updating || !data?.enabled || !active || accessDenied}
            onPress={() => refresh.mutate()}
          />
        </View>
        {query.isPending && (
          <ActivityIndicator
            accessibilityLabel="正在读取行业研究"
            color={C.green}
            style={i.loading}
          />
        )}
        {query.isError && (
          <Text style={i.warning}>
            {industryError(query.error)}
            {data ? " 当前显示上次取得的数据。" : ""}
          </Text>
        )}
        {refresh.isError && (
          <Text style={i.warning}>
            采集请求未确认：{industryError(refresh.error)}{" "}
            请先刷新状态，确认是否已有任务。
          </Text>
        )}
        {tracking.isError && (
          <Text style={i.warning}>
            跟踪设置未更新：{industryError(tracking.error)}
          </Text>
        )}
        {(query.isError || (!query.isPending && !data)) && (
          <Action
            label="重新读取行业状态"
            onPress={reload}
            disabled={query.isFetching}
          />
        )}
        {data && (
          <>
            <Text style={s.muted}>
              {stale ? "上次页面状态" : "页面状态更新"}：
              {industryTime(data.as_of)}（北京时间）
            </Text>
            <Text style={[s.muted, { marginTop: 8 }]}>{data.scope}</Text>
            {!data.enabled && (
              <Notice>行业研究尚未启用。已保存的报告和原文仍可查看。</Notice>
            )}
            {job && (
              <View style={[s.note, { marginTop: 15 }]}>
                <Text style={i.subheading}>
                  {industryJobLabel(job, jobQuery.isError || stale)}
                </Text>
                {!!job.message && <Text style={s.body}>{job.message}</Text>}
                <Text selectable style={s.muted}>
                  任务 {job.id}
                </Text>
                <Text style={s.muted}>
                  采集结束后仍需核对来源状态；行业报告按其单独标注的截至时间阅读。
                </Text>
                {jobQuery.isError && (
                  <Text style={i.warning}>
                    {industryError(jobQuery.error, "job")}
                  </Text>
                )}
              </View>
            )}
            {!data.themes.length && (
              <Notice>尚未配置行业主题，当前没有可展示的行业报告。</Notice>
            )}
            {!!data.themes.length && (
              <ScrollView
                horizontal
                showsHorizontalScrollIndicator={false}
                contentContainerStyle={i.themeTabs}
              >
                {data.themes.map((item) => (
                  <Pressable
                    key={item.id}
                    accessibilityRole="tab"
                    accessibilityState={{ selected: theme?.id === item.id }}
                    onPress={() => setSelected(item.id)}
                    style={[s.pill, theme?.id === item.id && s.pillActive]}
                  >
                    <Text
                      style={[
                        s.pillText,
                        theme?.id === item.id && { color: C.paper },
                      ]}
                    >
                      {item.name}
                      {!item.enabled ? " · 已暂停" : ""}
                    </Text>
                  </Pressable>
                ))}
              </ScrollView>
            )}
            {theme && (
              <ThemeDetail
                key={theme.id}
                theme={theme}
                connection={connection}
                active={active}
                stale={stale}
                onOriginal={setOriginalId}
                trackingPending={tracking.isPending}
                onTracking={() =>
                  tracking.mutate({ id: theme.id, enabled: !theme.enabled })
                }
              />
            )}
            <View style={[i.card, { marginTop: 22 }]}>
              <Text style={i.heading}>来源与覆盖范围</Text>
              <Text style={s.muted}>
                采集正常只说明该来源可访问；数据完整性和结论可靠性需单独核对。
              </Text>
              {data.limitations.map((text, index) => (
                <Text key={index} style={[s.body, { marginTop: 10 }]}>
                  · {text}
                </Text>
              ))}
              {data.budget && (
                <View style={[s.note, { marginTop: 14 }]}>
                  <Text style={s.body}>
                    {data.budget.analysis_enabled
                      ? "今日"
                      : "自动分析未启用 · 今日"}
                    模型处理阶段 {data.budget.calls_today}/
                    {data.budget.max_calls_per_day}
                  </Text>
                  <Text style={s.muted}>
                    数据来源免费，模型沿用现有额度。每个处理阶段当前最多发起{" "}
                    {data.budget.provider_calls_per_stage_max} 次底层模型调用；
                    {data.budget.max_calls_per_day}{" "}
                    是处理阶段上限，不是实际模型请求上限。
                  </Text>
                </View>
              )}
              {!data.sources.length && (
                <Text style={i.warning}>尚无已配置的信息源。</Text>
              )}
              <Action
                label={`${sourcesOpen ? "收起" : "查看"}来源状态 · ${data.sources.length} 个`}
                onPress={() => setSourcesOpen(!sourcesOpen)}
              />
              {sourcesOpen &&
                data.sources.map((source) => (
                  <SourceRow key={source.id} source={source} stale={stale} />
                ))}
            </View>
          </>
        )}
      </ScrollView>
      {originalId && !accessDenied && (
        <EvidenceModal
          connection={connection}
          id={originalId}
          onClose={() => setOriginalId(null)}
        />
      )}
    </>
  );
}

function ThemeDetail({
  theme,
  connection,
  active,
  stale,
  onOriginal,
  trackingPending,
  onTracking,
}: {
  theme: IndustryTheme;
  connection: Connection;
  active: boolean;
  stale: boolean;
  onOriginal: (id: string) => void;
  trackingPending: boolean;
  onTracking: () => void;
}) {
  const shown = visibleAssessment(theme);
  const [previousOpen, setPreviousOpen] = useState(false);
  const [companiesOpen, setCompaniesOpen] = useState(false);
  const currentStatus =
    theme.analysis_status?.status || theme.assessment?.status;
  return (
    <View>
      <View style={i.card}>
        <View style={s.spread}>
          <Text style={[i.heading, { flex: 1 }]}>{theme.name}</Text>
          <Pressable
            accessibilityRole="switch"
            accessibilityLabel={`${theme.enabled ? "暂停" : "开启"}${theme.name}跟踪`}
            accessibilityState={{
              checked: theme.enabled,
              disabled: trackingPending,
            }}
            disabled={trackingPending}
            onPress={onTracking}
            style={[
              s.smallButton,
              theme.enabled && { backgroundColor: C.dark },
              { minHeight: 44, justifyContent: "center" },
            ]}
          >
            <Text style={{ color: theme.enabled ? C.paper : C.green }}>
              {trackingPending
                ? "正在保存"
                : theme.enabled
                  ? "跟踪中"
                  : "开启跟踪"}
            </Text>
          </Pressable>
        </View>
        <Text style={s.body}>{theme.description}</Text>
        {!theme.enabled && (
          <Text style={i.warning}>
            已暂停跟踪，以下为已保存的证据与历史报告。
          </Text>
        )}
        <Text style={i.eyebrow}>
          {stale ? "上次状态 · " : ""}
          {industryStateLabel(theme.state)} ·{" "}
          {assessmentStatusLabel(currentStatus)}
        </Text>
        <Text style={s.muted}>
          {theme.evidence_count} 条证据 · {theme.independent_sources} 个独立来源
        </Text>
        <Text style={s.muted}>
          最新证据：{industryTime(theme.latest_evidence_at)}
        </Text>
        <Text style={i.subheading}>待验证假说</Text>
        <Text selectable style={s.body}>
          {theme.hypothesis}
        </Text>
        <Text style={i.subheading}>观察股票</Text>
        <Text style={s.muted}>用于追踪业务与行业传导，不代表买卖建议。</Text>
        {theme.companies.length ? (
          (companiesOpen ? theme.companies : theme.companies.slice(0, 4)).map(
            (company) => (
              <View key={company.id} style={i.company}>
                <Text style={s.body}>{company.name}</Text>
                <Text selectable style={s.muted}>
                  {company.exchange} · {company.ticker || "代码未提供"}
                </Text>
              </View>
            ),
          )
        ) : (
          <Text style={s.muted}>尚未配置观察公司。</Text>
        )}
        {theme.companies.length > 4 && (
          <Action
            label={
              companiesOpen
                ? "收起观察名单"
                : `查看全部 ${theme.companies.length} 家观察公司`
            }
            onPress={() => setCompaniesOpen(!companiesOpen)}
          />
        )}
        <Text style={i.subheading}>待验证经营指标</Text>
        {theme.indicators.map((text, n) => (
          <Text key={n} style={s.body}>
            · {text}
          </Text>
        ))}
        {!theme.indicators.length && (
          <Text style={s.muted}>尚未配置经营指标。</Text>
        )}
        <Text style={i.subheading}>反证条件与风险</Text>
        {theme.risks.map((text, n) => (
          <Text key={n} style={s.body}>
            · {text}
          </Text>
        ))}
        {!theme.risks.length && (
          <Text style={s.muted}>尚未提供反证条件，不能据此视为没有风险。</Text>
        )}
      </View>
      {shown.assessment ? (
        <AssessmentCard
          assessment={shown.assessment}
          onOriginal={onOriginal}
          title={
            shown.previous || (currentStatus && currentStatus !== "ready")
              ? "上次已完成报告"
              : "行业判断"
          }
          stale={stale}
        />
      ) : (
        <Notice>
          {assessmentStatusLabel(currentStatus)}
          。当前可先查阅原始证据；材料不足时不生成方向结论。
        </Notice>
      )}
      {!shown.previous && theme.previous_assessment?.status === "ready" && (
        <>
          <Action
            label={previousOpen ? "收起上次报告" : "与上次报告对照"}
            onPress={() => setPreviousOpen(!previousOpen)}
          />
          {previousOpen && (
            <AssessmentCard
              assessment={theme.previous_assessment}
              onOriginal={onOriginal}
              title="上次报告"
              stale={stale}
            />
          )}
        </>
      )}
      <EvidenceList
        connection={connection}
        theme={theme}
        active={active}
        onOriginal={onOriginal}
      />
    </View>
  );
}

function AssessmentCard({
  assessment,
  onOriginal,
  title,
  stale,
}: {
  assessment: IndustryAssessment;
  onOriginal: (id: string) => void;
  title: string;
  stale: boolean;
}) {
  return (
    <View style={[i.card, { marginTop: 16 }]}>
      <Text style={i.heading}>
        {title} · {industryStateLabel(assessment.state)}
      </Text>
      <Text style={s.muted}>
        报告截至：{industryTime(assessment.as_of)}（北京时间）
      </Text>
      <Text style={s.muted}>生成于：{industryTime(assessment.created_at)}</Text>
      <Text style={[s.muted, { marginTop: 7 }]}>
        {assessmentFreshness(assessment.as_of)}
        {stale ? "。当前连接异常，尚未核对最新报告。" : ""}
      </Text>
      <MathText
        style={[s.body, { marginTop: 18 }]}
        text={assessment.summary_zh || "报告未提供总结。"}
      />
      <Text style={[s.muted, { marginTop: 10 }]}>
        观察期限：{assessment.horizon || "未提供"}
      </Text>
      <ClaimList
        title="支持证据"
        claims={assessment.supporting}
        onOriginal={onOriginal}
        evidenceIds={assessment.evidence_ids}
      />
      <ClaimList
        title="相反证据"
        claims={assessment.opposing}
        onOriginal={onOriginal}
        evidenceIds={assessment.evidence_ids}
      />
      <ClaimList
        title="投资影响与传导"
        claims={assessment.investment_implications}
        onOriginal={onOriginal}
        evidenceIds={assessment.evidence_ids}
      />
      <Text style={s.muted}>以上描述产业与经营传导，不是股票涨跌概率。</Text>
      <ClaimList
        title="下一步观察"
        claims={assessment.watch_items}
        onOriginal={onOriginal}
        evidenceIds={assessment.evidence_ids}
      />
      <Text style={i.subheading}>尚不确定</Text>
      {assessment.unknowns.length ? (
        assessment.unknowns.map((text, n) => (
          <Text key={n} selectable style={s.body}>
            · {text}
          </Text>
        ))
      ) : (
        <Text style={s.muted}>报告未列出未知项，不代表结论已经确定。</Text>
      )}
      <Text style={i.subheading}>报告引用原文</Text>
      {assessment.evidence_ids.length ? (
        <View style={i.references}>
          {assessment.evidence_ids.map((id, n) => (
            <Action
              key={id}
              label={`证据 ${n + 1} · ${id.slice(0, 8)}`}
              onPress={() => onOriginal(id)}
            />
          ))}
        </View>
      ) : (
        <Text style={s.muted}>这份报告未提供可定位的原文引用。</Text>
      )}
    </View>
  );
}

function ClaimList({
  title,
  claims,
  evidenceIds,
  onOriginal,
}: {
  title: string;
  claims: IndustryClaim[];
  evidenceIds: string[];
  onOriginal: (id: string) => void;
}) {
  return (
    <View>
      <Text style={i.subheading}>{title}</Text>
      {claims.length ? (
        claims.map((claim, index) => (
          <View key={index} style={{ marginBottom: 9 }}>
            <Text selectable style={s.body}>
              · {claim.text_zh}
            </Text>
            <View style={i.references}>
              {claim.source_ids.map((id) =>
                evidenceIds.includes(id) ? (
                  <Action
                    key={id}
                    label={`原文 · ${id.slice(0, 8)}`}
                    onPress={() => onOriginal(id)}
                  />
                ) : (
                  <Text key={id} style={s.muted}>
                    引用不在本报告的证据范围内，待核对。
                  </Text>
                ),
              )}
            </View>
          </View>
        ))
      ) : (
        <Text style={s.muted}>当前报告未提供此项证据。</Text>
      )}
    </View>
  );
}

function EvidenceList({
  connection,
  theme,
  active,
  onOriginal,
}: {
  connection: Connection;
  theme: IndustryTheme;
  active: boolean;
  onOriginal: (id: string) => void;
}) {
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setSearch(input.trim()), 300);
    return () => clearTimeout(timer);
  }, [input]);
  const query = useInfiniteQuery({
    queryKey: [...industryQueryKey(connection), "evidence", theme.id, search],
    initialPageParam: 0,
    queryFn: ({ signal, pageParam }) =>
      fetchIndustryEvidence(connection, {
        theme: theme.id,
        offset: pageParam,
        q: search,
        signal,
      }),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0);
      return last.items.length && loaded < last.total ? loaded : undefined;
    },
    enabled: active,
    retry: retryRead,
  });
  const items = industryAccessDenied(query.error)
    ? []
    : [
        ...new Map(
          (query.data?.pages.flatMap((page) => page.items) || []).map(
            (item) => [item.id, item],
          ),
        ).values(),
      ];
  return (
    <View style={{ marginTop: 25 }}>
      <Text style={i.heading}>原始证据</Text>
      <Text style={s.muted}>
        原文按来源保存。首次收录时间不等同于发布时间；非中文内容保留原文。
      </Text>
      <TextInput
        value={input}
        onChangeText={setInput}
        style={[s.input, { marginVertical: 14 }]}
        placeholder="搜索本主题的原文"
        placeholderTextColor={C.muted}
        accessibilityLabel="搜索行业证据"
        returnKeyType="search"
        onSubmitEditing={() => setSearch(input.trim())}
      />
      {query.isPending && (
        <ActivityIndicator color={C.green} style={i.loading} />
      )}
      {query.isError && (
        <>
          <Text style={i.warning}>
            {industryError(query.error, "evidence")}
            {items.length ? " 以下为上次加载的原文。" : ""}
          </Text>
          <Action
            label="重新读取原文"
            onPress={() => {
              void query.refetch();
            }}
            disabled={query.isFetching}
          />
        </>
      )}
      {!query.isPending && !query.isError && !items.length && (
        <Notice>
          {search
            ? "没有匹配的原始证据。"
            : "尚未收录这个主题的证据。可以采集最新证据，再核对来源状态。"}
        </Notice>
      )}
      {items.map((item) => (
        <View key={item.id} style={[i.card, { marginTop: 12 }]}>
          <Text style={i.eyebrow}>{item.source_name}</Text>
          <Text selectable style={i.evidenceTitle}>
            {item.title || "未提供标题"}
          </Text>
          <Text style={s.muted}>{evidencePublishedLabel(item)}</Text>
          <Text style={s.muted}>
            首次收录：{industryTime(item.first_seen_at)} · 已保存版本{" "}
            {item.revision}
          </Text>
          {item.partial && (
            <Text style={i.warning}>仅取得部分正文，信息可能不完整。</Text>
          )}
          <Text numberOfLines={3} style={[s.body, { marginTop: 10 }]}>
            {item.text || "此记录没有正文，请打开来源网页核对。"}
          </Text>
          <Action label="展开已保存原文" onPress={() => onOriginal(item.id)} />
        </View>
      ))}
      {query.hasNextPage && (
        <Action
          label={query.isFetchingNextPage ? "正在加载…" : "加载更多证据"}
          disabled={query.isFetchingNextPage}
          onPress={() => {
            void query.fetchNextPage();
          }}
        />
      )}
    </View>
  );
}

function SourceRow({
  source,
  stale,
}: {
  source: IndustrySource;
  stale: boolean;
}) {
  return (
    <View style={i.source}>
      <Text style={i.subheading}>{source.name}</Text>
      <Text style={[s.muted, source.status === "error" && { color: C.accent }]}>
        {stale ? "上次状态 · " : ""}
        {industrySourceLabel(source)}
      </Text>
      <Text style={s.body}>{source.description}</Text>
      {!!source.limitations && (
        <Text style={[s.muted, { marginTop: 6 }]}>
          覆盖限制：{source.limitations}
        </Text>
      )}
      {!!source.message && <Text style={s.muted}>{source.message}</Text>}
      <Text style={s.muted}>
        已收录 {source.item_count} 条 · 计划间隔{" "}
        {source.refresh_minutes > 0
          ? `${source.refresh_minutes} 分钟`
          : "未提供"}
      </Text>
      <Text style={s.muted}>
        最近尝试：{industryTime(source.last_attempt_at)}
      </Text>
      <Text style={s.muted}>
        最近成功：{industryTime(source.last_success_at)}
      </Text>
      <WebLink url={source.url} label="打开信息源" />
    </View>
  );
}

function WebLink({ url, label }: { url: string; label: string }) {
  const [error, setError] = useState("");
  const safeURL = originalWebURL(url);
  if (!safeURL) return <Text style={s.muted}>未提供可打开的网页地址。</Text>;
  return (
    <View>
      <Action
        label={label}
        onPress={() => {
          setError("");
          void Linking.openURL(safeURL).catch(() =>
            setError("无法打开网页，请检查浏览器设置。"),
          );
        }}
      />
      {!!error && <Text style={i.warning}>{error}</Text>}
    </View>
  );
}

function EvidenceModal({
  connection,
  id,
  onClose,
}: {
  connection: Connection;
  id: string;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: [...industryQueryKey(connection), "evidence-detail", id],
    queryFn: ({ signal }) => fetchIndustryEvidenceById(connection, id, signal),
    retry: retryRead,
  });
  const evidence: IndustryEvidence | undefined =
    query.error instanceof APIError ? undefined : query.data;
  return (
    <Modal
      visible
      animationType="slide"
      onRequestClose={onClose}
      presentationStyle="pageSheet"
    >
      <SafeAreaView style={s.screen}>
        <View style={[s.container, { flex: 1 }]}>
          <View style={[s.spread, s.pad]}>
            <Text style={i.subheading}>已保存原文</Text>
            <Action label="关闭" onPress={onClose} />
          </View>
          <ScrollView contentContainerStyle={i.screen}>
            {query.isPending && (
              <ActivityIndicator color={C.green} style={i.loading} />
            )}
            {query.isError && (
              <>
                <Text style={i.warning}>
                  {industryError(query.error, "evidence")}
                  {evidence ? " 当前显示上次保存的原文。" : ""}
                </Text>
                <Action
                  label="重试读取原文"
                  onPress={() => {
                    void query.refetch();
                  }}
                  disabled={query.isFetching}
                />
              </>
            )}
            {evidence && (
              <>
                <Text style={i.eyebrow}>{evidence.source_name}</Text>
                <Text selectable style={s.h2}>
                  {evidence.title || "未提供标题"}
                </Text>
                <Text style={[s.muted, { marginTop: 12 }]}>
                  {evidencePublishedLabel(evidence)}
                </Text>
                <Text style={s.muted}>
                  首次收录：{industryTime(evidence.first_seen_at)}（北京时间）
                </Text>
                <Text selectable style={s.muted}>
                  证据 ID：{evidence.id} · 已保存版本 {evidence.revision}
                </Text>
                {evidence.partial && (
                  <Notice>
                    此处仅保存了部分正文。引用时需注意上下文缺口，并打开网页核对。
                  </Notice>
                )}
                <WebLink url={evidence.url} label="打开原始网页" />
                <Text selectable style={[s.body, { marginTop: 20 }]}>
                  {evidence.text || "尚未保存正文，请打开原始网页查看。"}
                </Text>
              </>
            )}
          </ScrollView>
        </View>
      </SafeAreaView>
    </Modal>
  );
}

const i = StyleSheet.create({
  screen: { paddingHorizontal: 24, paddingTop: 24, paddingBottom: 40 },
  heading: {
    color: C.ink,
    fontSize: 20,
    lineHeight: 30,
    fontWeight: "700",
    marginBottom: 8,
  },
  subheading: {
    color: C.ink,
    fontSize: 15,
    lineHeight: 24,
    fontWeight: "700",
    marginTop: 17,
    marginBottom: 5,
  },
  eyebrow: {
    color: C.green,
    fontSize: 12,
    lineHeight: 21,
    fontWeight: "600",
    marginVertical: 8,
  },
  action: {
    minHeight: 44,
    justifyContent: "center",
    alignSelf: "flex-start",
    paddingHorizontal: 5,
  },
  actionText: {
    color: C.green,
    fontSize: 13,
    lineHeight: 21,
    fontWeight: "600",
  },
  loading: { marginVertical: 30 },
  warning: { color: C.accent, fontSize: 13, lineHeight: 23, marginVertical: 9 },
  card: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 17,
    padding: 18,
    backgroundColor: C.paper,
  },
  themeTabs: { gap: 9, paddingVertical: 24 },
  company: { borderBottomWidth: 1, borderColor: C.line, paddingVertical: 8 },
  references: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: 12,
  },
  evidenceTitle: {
    color: C.ink,
    fontSize: 18,
    lineHeight: 28,
    fontWeight: "600",
    marginBottom: 10,
  },
  source: {
    borderTopWidth: 1,
    borderColor: C.line,
    paddingTop: 3,
    marginTop: 9,
  },
});
