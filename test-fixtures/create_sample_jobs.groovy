/*
 * Test fixture — run in a CONTROLLER script console (NOT the OC):
 *     <CONTROLLER_URL>/manage/script
 *     e.g. https://ci.rye.ps.beescloud.com/releases-v2609a/manage/script
 *
 * Populates a controller with a representative, folder-nested job ecosystem so the
 * inventory's per-controller pass (job types, SCM repo, triggers, build frequency) has
 * data to collect once you point the inventory at a folder-nested controller endpoint.
 *
 * Creates (idempotent — existing items are reused):
 *     team-platform/                         (folder)
 *       freestyle-build                      Freestyle + SCMTrigger, git SCM
 *       declarative-pipeline                 Pipeline (inline Declarative)
 *       scripted-pipeline                    Pipeline (inline Scripted)
 *     team-data/
 *       nightly-etl                          Freestyle + TimerTrigger
 *     standalone-pipeline                    Pipeline at the top level
 *
 * It also fires one build of each job (best-effort) so builds/week is non-zero.
 */
import jenkins.model.Jenkins
import com.cloudbees.hudson.plugins.folder.Folder
import hudson.model.FreeStyleProject
import hudson.tasks.Shell
import hudson.triggers.SCMTrigger
import hudson.triggers.TimerTrigger
import hudson.plugins.git.GitSCM
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition

def jenkins = Jenkins.instance
def log = []

def ensureFolder = { parent, String name ->
    def existing = parent.getItem(name)
    if (existing instanceof Folder) { return existing }
    if (existing != null) { throw new IllegalStateException("'${name}' exists but is not a Folder") }
    def f = parent.createProject(Folder, name)
    log << "folder     ${f.fullName}"
    return f
}

// Single-arg GitSCM(String) is stable across git-plugin versions, unlike the multi-arg form.
def gitScm = { String url -> new GitSCM(url) }

def ensureFreestyle = { parent, String name, Closure configure ->
    def existing = parent.getItem(name)
    if (existing != null) { log << "freestyle  ${existing.fullName} (exists)"; return existing }
    def p = parent.createProject(FreeStyleProject, name)
    configure(p)
    p.save()
    log << "freestyle  ${p.fullName}"
    return p
}

def ensurePipeline = { parent, String name, String script ->
    def existing = parent.getItem(name)
    if (existing != null) { log << "pipeline   ${existing.fullName} (exists)"; return existing }
    def p = parent.createProject(WorkflowJob, name)
    p.setDefinition(new CpsFlowDefinition(script, true))
    p.save()
    log << "pipeline   ${p.fullName}"
    return p
}

def platform = ensureFolder(jenkins, 'team-platform')
def data     = ensureFolder(jenkins, 'team-data')

ensureFreestyle(platform, 'freestyle-build') { p ->
    p.setScm(gitScm('https://github.com/example-org/platform-app.git'))
    p.addTrigger(new SCMTrigger('H/15 * * * *'))
    p.getBuildersList().add(new Shell('echo building platform-app'))
}

ensurePipeline(platform, 'declarative-pipeline', '''\
pipeline {
    agent any
    stages {
        stage('build') { steps { echo 'declarative build' } }
    }
}
''')

ensurePipeline(platform, 'scripted-pipeline', '''\
node {
    stage('build') { echo 'scripted build' }
}
''')

ensureFreestyle(data, 'nightly-etl') { p ->
    p.addTrigger(new TimerTrigger('H 2 * * *'))
    p.getBuildersList().add(new Shell('echo running nightly etl'))
}

ensurePipeline(jenkins, 'standalone-pipeline', '''\
pipeline {
    agent any
    stages { stage('noop') { steps { echo 'top-level pipeline' } } }
}
''')

// Best-effort: trigger one build each so builds-in-window / builds-per-week is non-zero.
def toBuild = ['team-platform/freestyle-build', 'team-platform/declarative-pipeline',
               'team-platform/scripted-pipeline', 'team-data/nightly-etl', 'standalone-pipeline']
toBuild.each { fn ->
    try {
        def item = jenkins.getItemByFullName(fn)
        if (item != null && item.metaClass.respondsTo(item, 'scheduleBuild2', int)) {
            item.scheduleBuild2(0)
            log << "queued     ${fn}"
        } else {
            log << "queue SKIP ${fn} (not buildable)"
        }
    } catch (ex) { log << "queue FAIL ${fn}: ${ex.message}" }
}

println '=== sample job ecosystem ==='
log.each { println "  ${it}" }
println '\nDone. Jobs created on this controller. Re-run the inventory to see them under'
println 'this controller\'s jobs.csv / job-type-summary.csv.'
return null
