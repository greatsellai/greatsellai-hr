import { useEffect, useState } from "react";
import { Icon, type IconName } from "./icons";
import "./landing.css";

export const ROOT_WORKSPACE_BASE_PATH = "/greatsellhr";

const painPoints: Array<{
  icon: IconName;
  title: string;
  description: string;
}> = [
  {
    icon: "inbox",
    title: "简历散在邮箱和文件夹",
    description: "下载、改名、归档、去重，占用大量本该用于判断候选人的时间。",
  },
  {
    icon: "filter",
    title: "筛选条件全靠人记",
    description: "学历、技能和经历逐份核对，候选人一多就容易漏掉关键差异。",
  },
  {
    icon: "layers",
    title: "评分口径难以统一",
    description: "同一份简历，不同人看出不同结论，沟通和复核成本反而更高。",
  },
  {
    icon: "match",
    title: "JD 与简历反复对照",
    description: "匹配项、缺口和风险藏在两份长文本里，面试前还要重新整理。",
  },
];

const comparisons = [
  {
    label: "传统招聘方式",
    title: "时间花在翻资料",
    description: "每收到一批简历，就重新经历一次下载、阅读、摘录和对照。",
    image: "/landing/hr/comparison-manual.webp",
    imageAlt: "HR 人员手工翻阅大量简历的工作场景",
    items: ["手动下载附件再逐份归档", "通读简历才能提取关键信息", "依靠个人经验记录评分", "拿着 JD 再逐条核对"],
  },
  {
    label: "大卖数智 AI 招聘工作台",
    title: "时间用来做判断",
    description: "系统先整理候选人事实、匹配依据与待核实项，HR 直接进入判断。",
    image: "/landing/hr/comparison-ai-workbench.webp",
    imageAlt: "HR 人员使用 AI 招聘工作台比较候选人的场景",
    items: ["批量上传，进阶版支持邮箱自动入库", "AI 提取事实并关联原文证据", "按团队权重统一评分和总结", "输出 JD 匹配项、缺口与风险"],
    featured: true,
  },
];

const capabilitySteps = [
  {
    number: "01",
    title: "收进来",
    description: "批量上传简历，或由进阶版从招聘邮箱自动收取新附件；每份简历都有处理状态和来源记录。",
    image: "/landing/hr/capability-resume-inbox.webp",
    imageAlt: "HR 人员将收到的简历统一归入数字工作台",
  },
  {
    number: "02",
    title: "快速筛",
    description: "组合学历、院校、专业、技能、经历等条件，不读完整简历也能先找到更值得看的候选人。",
    image: "/landing/hr/capability-fast-filter.webp",
    imageAlt: "HR 人员通过清晰筛选信号缩小候选人范围",
  },
  {
    number: "03",
    title: "统一比",
    description: "用团队自定义权重评分并生成 AI 总结，再用确认版 JD 查看匹配项、缺口与风险。",
    image: "/landing/hr/capability-scoring-jd.webp",
    imageAlt: "HR 人员比较候选人评分与岗位要求",
  },
  {
    number: "04",
    title: "带依据推进",
    description: "招聘 Agent 帮助查询、比较和解释结果；每个结论都能回到原文，由 HR 决定是否推进。",
    image: "/landing/hr/capability-agent-evidence.webp",
    imageAlt: "HR 人员查看 AI 招聘助手给出的判断依据",
  },
];

const pricing = [
  {
    name: "基础版",
    price: "99",
    eyebrow: "适合需要统一筛选与判断口径的团队",
    items: ["简历库与批量上传", "多条件组合筛选", "自定义权重评分与 AI 总结", "JD 匹配与招聘 Agent"],
  },
  {
    name: "进阶版",
    price: "199",
    eyebrow: "适合希望减少简历整理与 JD 撰写工作的团队",
    featured: true,
    items: ["包含基础版全部能力", "招聘邮箱自动收简历", "AI-JD 撰写与确认", "30 天免费试用默认版本"],
  },
  {
    name: "专业版",
    price: "299",
    eyebrow: "适合希望把面试准备与 HRBP 参考串起来的团队",
    items: ["包含进阶版全部能力", "线上面试题撰写", "线下面试题撰写", "HRBP AI 面试结果参考"],
  },
];

const faq = [
  {
    question: "每天只看几十份简历，也值得使用吗？",
    answer: "值得。只要团队经常重复核对相似条件、整理候选人信息或与用人经理对齐口径，统一的简历库、筛选和判断依据就能减少重复工作。",
  },
  {
    question: "AI 给出的评分可以直接淘汰候选人吗？",
    answer: "不可以。评分、总结和匹配结果都是招聘辅助信息，系统不会自动淘汰、自动邀约或替团队作出录用决定。",
  },
  {
    question: "为什么 AI 的结论值得复核？",
    answer: "产品把可用结论与简历原文证据、岗位要求和对应版本关联起来。HR 可以查看结论从何而来，也能看到不确定和需要进一步核实的部分。",
  },
  {
    question: "30 天免费试用包含什么？",
    answer: "新账号默认获得 30 天进阶版试用，可体验简历库、筛选、评分、AI 总结、JD 匹配、招聘 Agent、邮箱入库与 AI-JD 等已开放能力。",
  },
  {
    question: "不同团队的简历会被混在一起吗？",
    answer: "不会。注册后会建立独立工作区，候选人、原始文件和 AI 结论都按工作区隔离，并通过登录会话访问。",
  },
  {
    question: "三个版本有什么区别？",
    answer: "基础版覆盖简历筛选、评分、总结、JD 匹配与招聘 Agent；进阶版增加邮箱自动收简历和 AI-JD；专业版进一步覆盖线上、线下面试题与 HRBP 面试参考。",
  },
];

function setMeta(selector: string, content: string) {
  document.querySelector<HTMLMetaElement>(selector)?.setAttribute("content", content);
}

function setCanonical(url: string) {
  document.querySelector<HTMLLinkElement>('link[rel="canonical"]')?.setAttribute("href", url);
}

function BrandLogo({ tone = "dark" }: { tone?: "dark" | "light" }) {
  const [failed, setFailed] = useState(false);
  const source = tone === "dark"
    ? "/brand/greatsell-logo-cn-white.png"
    : "/brand/greatsell-logo-cn-color.png";

  return (
    <span className={`landing-brand landing-brand-${tone}`} aria-label="大卖数智 GreatSell AI">
      {failed ? (
        <span className="landing-brand-fallback" aria-hidden="true">
          <i />大卖数智
        </span>
      ) : (
        <img
          alt="大卖数智 GreatSell AI"
          onError={() => setFailed(true)}
          src={source}
        />
      )}
    </span>
  );
}

export function LandingPage({
  loginHref,
  registerHref,
}: {
  loginHref: string;
  registerHref: string;
}) {
  useEffect(() => {
    const canonical = "https://hr.greatsellai.net/";
    const description = "大卖数智 AI 招聘工作台帮助 HR 自动收集简历、快速筛选、统一评分并查看 JD 匹配依据，让招聘团队更快作出下一步判断。";
    document.title = "大卖数智 AI 招聘工作台｜AI 简历筛选、评分与 JD 匹配";
    setMeta('meta[name="description"]', description);
    setMeta('meta[property="og:title"]', "大卖数智 AI 招聘工作台");
    setMeta('meta[property="og:description"]', "让每一次招聘决策，都拥有AI驱动的判断能力。");
    setMeta('meta[property="og:url"]', canonical);
    setMeta('meta[property="og:image"]', `${canonical}landing/hr/og-hr-recruiting.webp`);
    setMeta('meta[name="twitter:title"]', "大卖数智 AI 招聘工作台");
    setMeta('meta[name="twitter:description"]', "让 HR 少翻资料，更快看清候选人是否值得推进。");
    setMeta('meta[name="twitter:image"]', `${canonical}landing/hr/og-hr-recruiting.webp`);
    setCanonical(canonical);
  }, []);

  return (
    <div className="landing-page">
      <header className="landing-header">
        <div className="landing-header-shell">
          <a className="landing-brand-link" href="#top" aria-label="返回首页">
            <BrandLogo />
          </a>
          <nav className="landing-nav" aria-label="首页导航">
            <a href="#pain-points">HR 痛点</a>
            <a href="#comparison">工作方式</a>
            <a href="#capabilities">产品能力</a>
            <a href="#pricing">版本定价</a>
            <a href="#faq">常见问题</a>
          </nav>
          <div className="landing-header-actions">
            <a className="landing-header-login" href={loginHref}>登录系统</a>
            <a className="landing-header-trial" href={registerHref}>免费试用 30 天 <Icon name="arrow-right" size={16} /></a>
          </div>
        </div>
      </header>

      <main id="main-content">
        <section className="landing-hero" id="top">
          <div className="landing-hero-overlay" />
          <div className="landing-shell landing-hero-grid">
            <div className="landing-hero-copy">
              <p className="landing-product-badge">大卖数智 AI 招聘工作台</p>
              <h1>让每一次招聘决策，<br />都拥有AI驱动的判断能力。</h1>
              <p className="landing-hero-description">从邮箱和文件夹里的成堆简历，到一眼看清候选人是否值得推进。自动收集、快速筛选、统一评分，并把每个结论对应到简历原文和岗位要求，让 HR 少翻资料，更快做出下一步判断。</p>
              <div className="landing-hero-actions">
                <a className="landing-button landing-button-primary" href={registerHref}>免费试用 30 天 <Icon name="arrow-right" size={19} /></a>
                <a className="landing-button landing-button-dark" href={loginHref}>已有账号，登录系统</a>
              </div>
              <ul className="landing-promises" aria-label="产品承诺">
                <li><Icon name="check" size={16} />30 天进阶版免费试用</li>
                <li><Icon name="check" size={16} />结论可回溯原文</li>
                <li><Icon name="check" size={16} />AI 辅助，HR 最终决定</li>
              </ul>
            </div>

            <div className="landing-decision-shell" aria-label="招聘判断流程示意">
              <div className="landing-decision-panel">
                <div className="landing-decision-heading">
                  <div>
                    <span className="landing-live-dot" />AI 正在整理候选人
                    <p>岗位：跨境电商运营</p>
                  </div>
                  <span>工作台示意</span>
                </div>
                <div className="landing-decision-flow">
                  <div><span>01</span><p>简历已入库</p><strong>48</strong></div>
                  <i />
                  <div><span>02</span><p>符合硬条件</p><strong>12</strong></div>
                  <i />
                  <div className="is-highlighted"><span>03</span><p>建议优先看</p><strong>4</strong></div>
                </div>
                <div className="landing-evidence-preview">
                  <p><Icon name="spark" size={16} />判断依据已整理</p>
                  <ul>
                    <li><span />岗位经验与核心技能匹配</li>
                    <li><span />评分口径已按团队模板统一</li>
                    <li className="needs-attention"><span />3 项信息建议在面试中核实</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="landing-section landing-pain-section" id="pain-points" aria-labelledby="pain-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">HR 真正浪费的时间</p>
            <div className="landing-section-heading">
              <h2 id="pain-title">不是不会判断，<br />而是每次判断前都要重新翻一遍资料</h2>
              <p>简历散、标准变、岗位多、用人经理催。真正拖慢招聘的，往往是找信息、对口径和补依据。</p>
            </div>
            <div className="landing-pain-grid">
              {painPoints.map((item) => (
                <article key={item.title}>
                  <span className="landing-icon-box"><Icon name={item.icon} size={23} /></span>
                  <h3>{item.title}</h3>
                  <p>{item.description}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-section landing-comparison-section" id="comparison" aria-labelledby="comparison-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">一批简历，两种速度</p>
            <div className="landing-section-heading">
              <h2 id="comparison-title">传统方式在翻资料，<br />AI 工作台直接整理判断依据</h2>
              <p>系统先完成信息整理与依据关联，HR 把注意力放回候选人是否值得推进。</p>
            </div>
            <div className="landing-comparison-grid">
              {comparisons.map((item) => (
                <article className={item.featured ? "is-featured" : ""} key={item.title}>
                  <div className="landing-image-frame">
                    <img alt={item.imageAlt} loading="lazy" src={item.image} />
                    <span>{item.label}</span>
                  </div>
                  <div className="landing-comparison-copy">
                    <h3>{item.title}</h3>
                    <p>{item.description}</p>
                    <ul>{item.items.map((entry) => <li key={entry}><Icon name="check" size={17} />{entry}</li>)}</ul>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-section landing-capabilities-section" id="capabilities" aria-labelledby="capabilities-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">一条更快的招聘判断路径</p>
            <div className="landing-section-heading">
              <h2 id="capabilities-title">从收到简历，到决定下一步，<br />都在同一个工作台完成</h2>
              <p>不是把一个 AI 按钮加进旧流程，而是让收集、筛选、比较和推进自然连接起来。</p>
            </div>
            <div className="landing-capability-grid">
              {capabilitySteps.map((item) => (
                <article key={item.number}>
                  <div className="landing-capability-image">
                    <img alt={item.imageAlt} loading="lazy" src={item.image} />
                    <span>{item.number}</span>
                  </div>
                  <div className="landing-capability-copy">
                    <h3>{item.title}</h3>
                    <p>{item.description}</p>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-trust-section" aria-labelledby="trust-title">
          <div className="landing-shell landing-trust-grid">
            <div>
              <p className="landing-section-kicker">AI 辅助，不替代 HR</p>
              <h2 id="trust-title">结论可以复核，<br />决定仍然属于招聘团队</h2>
            </div>
            <ul>
              <li><Icon name="check" size={20} /><span><strong>每个结论都能找到依据</strong>可用结论关联简历原文、岗位要求和对应版本，减少“AI 为什么这么说”的疑问。</span></li>
              <li><Icon name="check" size={20} /><span><strong>不把敏感属性当作岗位条件</strong>不以性别、年龄、地域、婚育等敏感信息做筛选或排序。</span></li>
              <li><Icon name="check" size={20} /><span><strong>系统不替团队作人事决定</strong>不自动淘汰、自动邀约或自动录用，HR 始终是最终决策者。</span></li>
            </ul>
          </div>
        </section>

        <section className="landing-section landing-pricing-section" id="pricing" aria-labelledby="pricing-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">按团队当前阶段开始</p>
            <div className="landing-section-heading landing-pricing-heading">
              <h2 id="pricing-title">每月 ¥99 起，<br />把翻简历的时间还给招聘判断</h2>
              <p>新账号可先免费试用 30 天进阶版，再选择适合团队当前招聘流程的版本。</p>
            </div>
            <div className="landing-pricing-grid">
              {pricing.map((plan) => (
                <article className={plan.featured ? "is-featured" : ""} key={plan.name}>
                  {plan.featured && <span className="landing-plan-badge">推荐</span>}
                  <p className="landing-plan-name">{plan.name}</p>
                  <p className="landing-plan-eyebrow">{plan.eyebrow}</p>
                  <p className="landing-plan-price"><b>¥{plan.price}</b><span>/ 月</span></p>
                  <ul>{plan.items.map((item) => <li key={item}><Icon name="check" size={17} />{item}</li>)}</ul>
                  <a className={plan.featured ? "landing-button landing-button-primary" : "landing-button landing-button-outline"} href={registerHref}>先免费试用 30 天 <Icon name="arrow-right" size={17} /></a>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-section landing-faq-section" id="faq" aria-labelledby="faq-title">
          <div className="landing-shell landing-faq-layout">
            <div>
              <p className="landing-section-kicker">开始之前</p>
              <h2 id="faq-title">HR 常问的<br />几个问题</h2>
              <a className="landing-inline-link" href={registerHref}>从第一份简历开始试用 <Icon name="arrow-right" size={17} /></a>
            </div>
            <div className="landing-faq-list">
              {faq.map((item, index) => (
                <details key={item.question} open={index === 0}>
                  <summary>{item.question}<Icon name="chevron-down" size={20} /></summary>
                  <p>{item.answer}</p>
                </details>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-final-cta" aria-labelledby="final-cta-title">
          <div className="landing-shell landing-final-cta-inner">
            <div>
              <p className="landing-section-kicker">从下一批简历开始</p>
              <h2 id="final-cta-title">把下一批简历，变成一份更快、更清楚的判断结果</h2>
              <p>免费试用 30 天进阶版，从上传第一份简历开始。</p>
            </div>
            <div className="landing-final-actions">
              <a className="landing-button landing-button-primary" href={registerHref}>免费试用 30 天 <Icon name="arrow-right" size={19} /></a>
              <a className="landing-button landing-button-dark" href={loginHref}>已有账号，登录系统</a>
            </div>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-shell landing-footer-main">
          <BrandLogo />
          <p>让每一次招聘决策，都拥有AI驱动的判断能力。</p>
          <nav aria-label="页脚导航"><a href="#capabilities">产品能力</a><a href="#pricing">版本定价</a><a href={loginHref}>登录系统</a></nav>
        </div>
        <div className="landing-shell landing-footer-bottom"><span>© 2026 大卖数智 GreatSell AI</span><span>AI 辅助判断，招聘团队最终决策</span></div>
      </footer>
    </div>
  );
}
