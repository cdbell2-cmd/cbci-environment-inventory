/*
 * Test fixture — run in the OPERATIONS CENTER (CJOC) script console:
 *     <CJOC_URL>/manage/script   (e.g. https://ci.rye.ps.beescloud.com/cjoc/manage/script)
 *
 * Creates folder-nested Managed Controllers so the environment inventory's controller-URL
 * resolution can be exercised against the real reported case:
 *
 *     CJOC item path : /cjoc/job/releases/job/v2609a/
 *     getFullName()  : releases/v2609a
 *     real endpoint  : https://<host>/releases-v2609a/     <-- folders flattened with '-'
 *
 * It builds a few shapes on purpose:
 *     releases/v2609a             (single folder  -> releases-v2609a)
 *     releases/v2609b             (sibling in same folder)
 *     team-a/qa/controller-x      (deep nesting   -> team-a-qa-controller-x)
 *     top-level-controller        (no folder, regression guard -> unchanged)
 *
 * Idempotent: existing folders/controllers are reused, not duplicated.
 * NOTE: this creates the controller ITEMS so discovery can read getFullName()/endpoint.
 * Provisioning/starting them (so /scriptText is reachable) is driven by your cluster's
 * managed-controller config — start them from the UI if you want the full per-controller
 * pass to succeed; discovery + URL resolution are exercised either way.
 */
import jenkins.model.Jenkins
import com.cloudbees.hudson.plugins.folder.Folder
import com.cloudbees.opscenter.server.model.ManagedMaster

def jenkins = Jenkins.instance
def created = []
def reused  = []

// Return (creating if needed) the Folder at a '/'-separated path under the OC root.
def ensureFolderPath
ensureFolderPath = { String path ->
    def parent = jenkins
    path.split('/').each { seg ->
        def existing = parent.getItem(seg)
        if (existing == null) {
            existing = parent.createProject(Folder, seg)
            created << "folder   ${existing.fullName}"
        } else if (!(existing instanceof Folder)) {
            throw new IllegalStateException("'${existing.fullName}' exists but is not a Folder")
        } else {
            reused << "folder   ${existing.fullName}"
        }
        parent = existing
    }
    return parent
}

def ensureController = { parent, String name ->
    def existing = parent.getItem(name)
    if (existing == null) {
        def mm = parent.createProject(ManagedMaster, name)
        created << "controller ${mm.fullName}"
    } else {
        reused << "controller ${existing.fullName}"
    }
}

// folderPath (null/'' = OC root) -> [controller names]
def layout = [
    'releases'   : ['v2609a', 'v2609b'],
    'team-a/qa'  : ['controller-x'],
    ''           : ['top-level-controller'],
]

layout.each { folderPath, names ->
    def parent = folderPath ? ensureFolderPath(folderPath) : jenkins
    names.each { ensureController(parent, it) }
}

jenkins.save()

println '=== created ==='
created.each { println "  + ${it}" }
println '=== reused (already present) ==='
reused.each  { println "  = ${it}" }

println '\n=== discovered ConnectedMasters (fullName -> expected endpoint segment) ==='
def host = (jenkins.rootUrl ?: 'http://HOST/').replaceAll('/+$', '').replaceAll('/cjoc$', '')
jenkins.getAllItems(com.cloudbees.opscenter.server.model.ConnectedMaster).each { cm ->
    def fn = cm.fullName
    def segment = fn.replace('/', '-')          // mirrors resolve_controller_url()
    println "  ${fn.padRight(32)} -> ${host}/${segment}"
}
println '\nDone. Now run:  python generate_environment_inventory.py --oc-url <CJOC_URL>'
return null
