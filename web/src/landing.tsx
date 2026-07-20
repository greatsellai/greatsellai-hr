import { useEffect } from "react";
import { Icon, type IconName } from "./icons";
import "./landing.css";

export const ROOT_WORKSPACE_BASE_PATH = "/greatsellhr";

const productCapabilities: Array<{
  icon: IconName;
  title: string;
  description: string;
}> = [
  {
    icon: "folder",
    title: "简历库与来源追溯",
    description: "统一沉淀简历版本、原始文件与 AI 结论，让每份候选人信息都有来源可查。",
  },
  {
    icon: "filter",
    title: "条件筛选",
    description: "围绕学历、经历、技能和关键词组合条件，快速定位符合岗位要求的候选人。",
  },
  {
    icon: "layers",
    title: "AI 评分与总结",
    description: "按团队自己的评分规则评估候选人，并生成便于招聘判断的结构化 AI 总结。",
  },
  {
    icon: "match",
    title: "JD 匹配与招聘 Agent",
    description: "将岗位要求与简历事实放在同一判断框架中，解释匹配、缺口与待核实项。",
  },
  {
    icon: "inbox",
    title: "自动收简历与 AI-JD",
    description: "自动汇集收件邮箱中的简历，并把岗位需求沉淀为可编辑、可复用的 JD。",
  },
  {
    icon: "briefcase",
    title: "面试题与 HRBP 参考",
    description: "将线上、线下面试准备和综合参考串成连续流程，帮助团队推进下一步。",
  },
];

const workflow = [
  {
    number: "01",
    title: "收集简历",
    text: "手动上传或从收件邮箱汇集候选人资料。",
  },
  {
    number: "02",
    title: "读懂简历",
    text: "校验文件、提取事实，并保留对应的原文证据。",
  },
  {
    number: "03",
    title: "比较候选人",
    text: "完成筛选、评分、总结与岗位 JD 匹配。",
  },
  {
    number: "04",
    title: "推进面试",
    text: "生成面试材料与 HRBP 参考，支持人做最终判断。",
  },
];

const pricing = [
  {
    name: "基础版",
    price: "99",
    eyebrow: "把简历看明白",
    items: ["简历筛选与简历库", "配置权重评分与 AI 总结", "JD 匹配评分", "招聘 Agent"],
  },
  {
    name: "高级版",
    price: "199",
    eyebrow: "让招聘输入更完整",
    featured: true,
    items: ["包含基础版全部能力", "收简历邮箱自动收集", "AI-JD 撰写", "更完整的岗位输入沉淀"],
  },
  {
    name: "专业版",
    price: "299",
    eyebrow: "把面试与判断串起来",
    items: ["包含高级版全部能力", "线上面试题撰写", "线下面试题撰写", "HRBP AI 面试结果参考"],
  },
];

const faq = [
  {
    question: "三个版本有什么区别？",
    answer: "基础版覆盖筛选、评分、总结、JD 匹配与招聘 Agent；高级版增加自动收简历和 AI-JD；专业版进一步覆盖线上、线下面试与 HRBP 参考。",
  },
  {
    question: "AI 会自动淘汰候选人吗？",
    answer: "不会。GreatSell AI 只辅助提取、分析和组织信息，最终招聘决策始终由招聘团队作出。",
  },
  {
    question: "系统如何保证招聘判断有依据？",
    answer: "产品把可用结论与简历原文证据、岗位要求和版本信息关联起来，帮助招聘人员理解结论从何而来。",
  },
  {
    question: "如何开始使用？",
    answer: "进入登录系统后即可开始使用工作台，建立岗位、收集简历并开展筛选与评估。",
  },
];

function setDescription(content: string) {
  const description = document.querySelector('meta[name="description"]');
  description?.setAttribute("content", content);
}

function BrandLogo() {
  return (
    <span className="landing-brand" aria-label="大卖数智 GreatSell AI">
      <span className="landing-brand-fallback" aria-hidden="true">
        <i />
        大卖数智
      </span>
      <img
        alt="大卖数智 GreatSell AI"
        onError={(event) => {
          event.currentTarget.style.display = "none";
        }}
        src="https://greatsell.ai/print-quote/brand/greatsell-logo-cn-color.png"
      />
    </span>
  );
}

export function LandingPage({ loginHref }: { loginHref: string }) {
  useEffect(() => {
    document.title = "大卖数智 GreatSell AI 招聘工作台";
    setDescription("让每一次招聘决策，都拥有AI驱动的判断能力。GreatSell AI 将简历、岗位与面试信息沉淀为可解释的招聘依据。");
  }, []);

  return (
    <div className="landing-page">
      <header className="landing-header">
        <div className="landing-shell landing-header-inner">
          <a className="landing-brand-link" href="#top" aria-label="返回首页">
            <BrandLogo />
          </a>
          <nav className="landing-nav" aria-label="首页导航">
            <a href="#capabilities">产品能力</a>
            <a href="#workflow">工作流程</a>
            <a href="#pricing">版本定价</a>
            <a href="#faq">常见问题</a>
          </nav>
          <a className="landing-login-link" href={loginHref}>登录系统 <Icon name="arrow-right" size={16} /></a>
        </div>
      </header>

      <main id="main-content">
        <section className="landing-hero" id="top">
          <div className="landing-shell landing-hero-grid">
            <div className="landing-hero-copy">
              <p className="landing-eyebrow"><span /> GREATSELL AI · 招聘工作台</p>
              <h1>让每一次招聘决策，<br />都拥有AI驱动的判断能力。</h1>
              <p className="landing-hero-description">从简历收集、来源校验与智能筛选，到评分、岗位匹配和面试参考，把分散的信息沉淀为可解释的招聘依据。</p>
              <div className="landing-hero-actions">
                <a className="landing-button landing-button-primary" href={loginHref}>登录工作台 <Icon name="arrow-right" size={18} /></a>
                <a className="landing-button landing-button-secondary" href="#pricing">查看版本方案</a>
              </div>
              <ul className="landing-promises" aria-label="产品承诺">
                <li><Icon name="check" size={16} />来源可追溯</li>
                <li><Icon name="check" size={16} />AI 辅助，人做决定</li>
                <li><Icon name="check" size={16} />一个工作台覆盖招聘流程</li>
              </ul>
            </div>

            <div className="landing-product-visual" aria-label="招聘工作台产品界面示意">
              <div className="landing-visual-glow" />
              <div className="landing-workbench">
                <div className="landing-workbench-rail">
                  <span className="landing-workbench-mark" />
                  <span className="is-active" /><span /><span /><span />
                </div>
                <div className="landing-workbench-content">
                  <div className="landing-workbench-topbar">
                    <strong>候选人工作台</strong>
                    <span>招聘助手</span>
                  </div>
                  <div className="landing-workbench-body">
                    <aside className="landing-filter-card">
                      <p>筛选条件</p>
                      <span>985 / 211 <b>不限</b></span>
                      <span>工作年限 <b>3 年+</b></span>
                      <span>核心技能 <b>Python</b></span>
                      <button type="button">应用筛选</button>
                    </aside>
                    <div className="landing-candidate-area">
                      <div className="landing-candidate-heading"><strong>候选人结果</strong><span>12 人匹配</span></div>
                      <article className="landing-candidate-card">
                        <div className="landing-avatar">陈</div>
                        <div><strong>陈昱</strong><p>数据分析师 · 4 年经验</p><em>Python</em><em>SQL</em></div>
                        <div className="landing-score"><b>86</b><span>匹配分</span></div>
                      </article>
                      <article className="landing-evidence-card">
                        <p><Icon name="spark" size={14} /> AI 判断依据</p>
                        <span>岗位经验匹配</span><span>技能命中 3 项</span><span>需核实管理经验</span>
                      </article>
                    </div>
                  </div>
                </div>
              </div>
              <p className="landing-visual-caption">产品界面示意</p>
            </div>
          </div>
        </section>

        <section className="landing-section landing-value-section" aria-labelledby="value-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">不是更多信息，而是更好的判断</p>
            <h2 id="value-title">让招聘团队看得更快，<br />判断得更清楚。</h2>
            <div className="landing-value-grid">
              <article><Icon name="document" size={22} /><h3>事实不再散落</h3><p>将简历、岗位与面试信息收进同一工作台，减少反复翻找与手工整理。</p></article>
              <article><Icon name="activity" size={22} /><h3>结论不再模糊</h3><p>筛选命中、评分依据和岗位缺口都有清晰说明，帮助团队快速复核。</p></article>
              <article><Icon name="user" size={22} /><h3>人始终在决策中</h3><p>AI 提供辅助判断和待核实项，不替代招聘人员对候选人的最终决定。</p></article>
            </div>
          </div>
        </section>

        <section className="landing-section landing-workflow-section" id="workflow" aria-labelledby="workflow-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">一条可追溯的招聘工作流</p>
            <div className="landing-section-heading"><h2 id="workflow-title">从收到简历，到做出更好的招聘判断。</h2><p>把招聘中的信息收集、分析、比较和面试推进连接起来，让每一步都更有依据。</p></div>
            <ol className="landing-workflow-list">
              {workflow.map((item) => <li key={item.number}><span>{item.number}</span><div><h3>{item.title}</h3><p>{item.text}</p></div></li>)}
            </ol>
          </div>
        </section>

        <section className="landing-section landing-capabilities-section" id="capabilities" aria-labelledby="capabilities-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">覆盖招聘判断的关键环节</p>
            <div className="landing-section-heading"><h2 id="capabilities-title">不是一项 AI 功能，<br />而是一套招聘工作方式。</h2><p>围绕招聘团队真实的工作流设计，让系统提供信息、依据和下一步，而不是替人做决定。</p></div>
            <div className="landing-capability-grid">
              {productCapabilities.map((item) => <article key={item.title}><span className="landing-capability-icon"><Icon name={item.icon} size={21} /></span><h3>{item.title}</h3><p>{item.description}</p><span className="landing-card-arrow"><Icon name="arrow-right" size={17} /></span></article>)}
            </div>
          </div>
        </section>

        <section className="landing-trust-section" aria-labelledby="trust-title">
          <div className="landing-shell landing-trust-grid">
            <div><p className="landing-section-kicker">可信的 AI 招聘辅助</p><h2 id="trust-title">AI 帮你看见依据，<br />招聘团队负责作出决定。</h2></div>
            <ul><li><Icon name="check" size={18} /><span><strong>以事实为基础</strong>可用结论关联简历原文、岗位要求与版本信息。</span></li><li><Icon name="check" size={18} /><span><strong>以岗位为边界</strong>不以性别、年龄、地域、婚育等敏感属性做筛选或排序。</span></li><li><Icon name="check" size={18} /><span><strong>以人为最终决策者</strong>系统不自动淘汰、自动邀约或自动作出录用决定。</span></li></ul>
          </div>
        </section>

        <section className="landing-section landing-pricing-section" id="pricing" aria-labelledby="pricing-title">
          <div className="landing-shell">
            <p className="landing-section-kicker">选择适合团队的招聘能力</p>
            <div className="landing-pricing-heading"><div><h2 id="pricing-title">从每月 ¥99 开始，<br />让招聘判断更有依据。</h2></div><p>三种版本覆盖从看简历、收简历到面试推进的完整招聘流程。</p></div>
            <div className="landing-pricing-grid">
              {pricing.map((plan) => <article className={plan.featured ? "is-featured" : ""} key={plan.name}>{plan.featured && <span className="landing-plan-badge">推荐</span>}<p className="landing-plan-name">{plan.name}</p><p className="landing-plan-eyebrow">{plan.eyebrow}</p><p className="landing-plan-price"><b>¥{plan.price}</b><span>/ 月</span></p><ul>{plan.items.map((item) => <li key={item}><Icon name="check" size={16} />{item}</li>)}</ul><a className={plan.featured ? "landing-button landing-button-primary" : "landing-button landing-button-secondary"} href={loginHref}>登录后开通 <Icon name="arrow-right" size={16} /></a></article>)}
            </div>
          </div>
        </section>

        <section className="landing-section landing-faq-section" id="faq" aria-labelledby="faq-title">
          <div className="landing-shell landing-faq-layout"><div><p className="landing-section-kicker">常见问题</p><h2 id="faq-title">开始之前，<br />你可能想知道这些。</h2><a className="landing-inline-link" href={loginHref}>登录系统开始使用 <Icon name="arrow-right" size={16} /></a></div><div className="landing-faq-list">{faq.map((item, index) => <details key={item.question} open={index === 0}><summary>{item.question}<Icon name="chevron-down" size={19} /></summary><p>{item.answer}</p></details>)}</div></div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-shell landing-footer-inner"><BrandLogo /><p>让每一次招聘决策，都拥有AI驱动的判断能力。</p><span>© 2026 大卖数智 GreatSell AI</span></div>
      </footer>
    </div>
  );
}
