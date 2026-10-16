package dev.maiui.agent;

import android.app.UiAutomation;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Rect;
import android.os.*;
import android.view.*;
import android.view.accessibility.*;
import android.util.Base64;
import org.json.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;

/** Development-only API34 executor. No fixture-specific setters or private app reads. */
public final class Main {
    static UiAutomation ax;
    static final Object gate = new Object();
    static final AtomicLong events = new AtomicLong();
    static final AtomicLong treeReads=new AtomicLong(), imageReads=new AtomicLong();
    static long consumed=-1, revision=0;
    static String last="";
    static volatile JSONObject cached;
    static Map<String,AccessibilityNodeInfo> handles=new HashMap<>();
    static int width=1080,height=1920;
    static final boolean deferredObservations="1".equals(System.getenv("MAI_UI_DEFER_OBSERVATION"));
    // Framework source IDs, never traversal paths or matching labels.
    static final String executorEpoch=UUID.randomUUID().toString();
    static long identityEpoch=0, nextNode=0;
    static String rootIdentity="";
    static volatile int activeWindow=-1;
    static boolean previousPartial=false;
    static final AtomicLong lostWindows=new AtomicLong();
    static long seenLostWindows=0;
    static Map<String,String> nativeIds=new HashMap<>();
    static java.lang.reflect.Method sourceId;
    static String nativeKey(AccessibilityNodeInfo n) {
        try {
            long source=(Long)sourceId.invoke(n);
            // Unknown virtual providers can reuse virtual IDs. Do not promise
            // lifetime identity unless they supply their own unique ID.
            int virtual=(int)(source >> 32);
            String unique=n.getUniqueId();
            if((int)source==Integer.MAX_VALUE || (virtual!=-1 && unique==null))return null;
            return n.getWindowId()+":"+source+":"+str(n.getPackageName())+":"+
                str(n.getClassName())+":"+str(n.getViewIdResourceName())+":"+unique;
        } catch(Exception unavailable) { return null; }
    }
    static boolean fenced=false, inputReleased=true;
    static final Map<String,JSONObject> ledger=new LinkedHashMap<>();

    static String str(CharSequence s){return s==null?"":s.toString();}
    static Map<String,String> processCredentials()throws Exception{
        Map<String,String> fields=new HashMap<>();
        for(String line:Files.readAllLines(Paths.get("/proc/thread-self/status"))){
            int colon=line.indexOf(':');if(colon>0)fields.put(line.substring(0,colon),line.substring(colon+1).trim());
        }
        return fields;
    }
    static void verifyShellCredentials()throws Exception{
        Map<String,String> fields=processCredentials();
        boolean safe=android.os.Process.myUid()==2000 && android.system.Os.getgid()==2000
            && "1".equals(fields.get("NoNewPrivs"));
        for(String field:Arrays.asList("Uid","Gid"))
            for(String id:fields.getOrDefault(field,"").split("\\s+"))safe &= "2000".equals(id);
        for(String field:Arrays.asList("CapInh","CapPrm","CapEff","CapAmb"))
            safe &= fields.getOrDefault(field,"").matches("0+");
        require(safe,"PERMISSION_DENIED: shell execution profile was not established");
    }
    static String content(AccessibilityNodeInfo n){return n.isShowingHintText()?"":str(n.getText());}
    static String role(AccessibilityNodeInfo n){
        String c=str(n.getClassName());
        if(n.isEditable())return "textbox";
        if(c.endsWith("Button"))return "button";
        if(c.endsWith("CheckBox")||c.endsWith("Switch"))return "checkbox";
        if(n.isScrollable())return "scrollarea";
        if(c.endsWith("TextView"))return "text";
        return "container";
    }
    static JSONObject bounds(Rect r)throws Exception{return new JSONObject().put("x",r.left).put("y",r.top).put("width",r.width()).put("height",r.height());}
    static void walk(AccessibilityNodeInfo n,String path,String parent,JSONArray nodes,Map<String,AccessibilityNodeInfo> refs,Map<String,String> identities,boolean[] flags,int depth)throws Exception{
        if(n==null){flags[0]=true;return;}
        if(nodes.length()>=1500||depth>40){flags[0]=true;return;}
        String key=nativeKey(n), id;
        if(key==null){flags[1]=false;id="path:"+path;}
        else {
            if(identities.containsKey(key)){flags[0]=true;return;}
            id=nativeIds.get(key);
            if(id==null)id="node:"+(++nextNode);
            identities.put(key,id);
        }
        Rect b=new Rect();n.getBoundsInScreen(b);
        String text=n.isPassword()?"":content(n), hint=str(n.getHintText()), name=str(n.getContentDescription());
        if(name.isEmpty())name=!text.isEmpty()?text:hint;
        String pkg=str(n.getPackageName());
        JSONArray acts=new JSONArray();for(AccessibilityNodeInfo.AccessibilityAction a:n.getActionList())acts.put(new JSONObject().put("id",Integer.toString(a.getId())).put("label",str(a.getLabel())));
        JSONObject row=new JSONObject().put("id",id).put("parent_id",parent==null?JSONObject.NULL:parent)
            .put("window_id","window-"+n.getWindowId()).put("app_id","app:"+pkg)
            .put("class_name",str(n.getClassName())).put("automation_id",n.getViewIdResourceName()==null?JSONObject.NULL:n.getViewIdResourceName())
            .put("role",role(n)).put("name",name).put("text",n.isPassword()?JSONObject.NULL:text)
            .put("hint",hint).put("text_status",n.isPassword()?"redacted":"readable").put("bounds",bounds(b))
            .put("enabled",n.isEnabled()).put("visible",n.isVisibleToUser()).put("focused",n.isFocused())
            .put("editable",n.isEditable()).put("password",n.isPassword()).put("checked",n.isCheckable()?n.isChecked():JSONObject.NULL).put("actions",acts)
            .put("selection_start",n.getTextSelectionStart()).put("selection_end",n.getTextSelectionEnd());
        JSONArray signature=new JSONArray().put(executorEpoch).put(identityEpoch);
        for(String field:Arrays.asList("id","parent_id","window_id","app_id","class_name","automation_id","role","name","text","hint","text_status","bounds","enabled","visible","editable","password","checked"))signature.put(row.opt(field));
        byte[] digest=java.security.MessageDigest.getInstance("SHA-256").digest(signature.toString().getBytes("UTF-8"));
        StringBuilder token=new StringBuilder();String hex="0123456789abcdef";for(byte b0:digest){token.append(hex.charAt((b0>>4)&15));token.append(hex.charAt(b0&15));}
        row.put("target_token",token.toString());
        nodes.put(row);refs.put(id,n);
        for(int i=0;i<n.getChildCount();i++)walk(n.getChild(i),path+"_"+i,id,nodes,refs,identities,flags,depth+1);
    }
    static JSONObject refresh()throws Exception{
        treeReads.incrementAndGet();
        synchronized(gate){
            long start=SystemClock.uptimeMillis(), seen=events.get();
            AccessibilityNodeInfo root=ax.getRootInActiveWindow();
            String rootKey=root==null?"":nativeKey(root);
            long lost=lostWindows.get();
            if(previousPartial || !Objects.equals(rootIdentity,rootKey) || lost!=seenLostWindows){
                identityEpoch++;nativeIds.clear();
            }
            rootIdentity=rootKey;seenLostWindows=lost;
            activeWindow=root==null?-1:root.getWindowId();
            JSONArray rows=new JSONArray();Map<String,AccessibilityNodeInfo> refs=new HashMap<>();
            Map<String,String> identities=new HashMap<>();boolean[] flags={root==null,true};
            if(root!=null){
                if(root.refresh())walk(root,"n0",null,rows,refs,identities,flags,0);
                else flags[0]=true;
            }
            nativeIds=identities;previousPartial=flags[0];
            String value=identityEpoch+":"+rows.toString();if(!value.equals(last)){revision++;last=value;}
            String pkg=root==null?"":str(root.getPackageName());
            JSONObject result=new JSONObject().put("revision",Long.toString(revision)).put("nodes",rows)
                .put("foreground_app_id",pkg.isEmpty()?JSONObject.NULL:"app:"+pkg)
                .put("window_id",root==null?JSONObject.NULL:"window-"+root.getWindowId())
                .put("captured_at",SystemClock.uptimeMillis()).put("scan_ms",SystemClock.uptimeMillis()-start)
                .put("width",width).put("height",height).put("truncated",flags[0])
                .put("identity_mode",flags[1]?"native_lifetime":"revision")
                .put("identity_epoch",executorEpoch+":"+identityEpoch);
            handles=refs;cached=result;consumed=seen;return result;
        }
    }
    static void require(boolean ok,String code)throws Exception{if(!ok)throw new Exception(code);}
    static AccessibilityNodeInfo target(JSONObject q)throws Exception{
        refresh();
        if(!q.getString("revision").equals(cached.getString("revision"))){
            boolean unchanged=false;
            if(cached.optString("identity_mode").equals("native_lifetime") && q.has("target_token")){
                JSONArray rows=cached.getJSONArray("nodes");
                for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);if(row.getString("id").equals(q.getString("node_id"))){unchanged=q.getString("target_token").equals(row.optString("target_token"));break;}}
            }
            require(unchanged,"STALE_TARGET: target changed or its identity was retired; emit fresh AX and choose again");
        }
        String scope=q.optString("app_id","");
        if(!scope.isEmpty())require(scope.equals(cached.optString("foreground_app_id")),"TARGET_NOT_FOREGROUND");
        AccessibilityNodeInfo n=handles.get(q.getString("node_id"));
        require(n!=null&&n.refresh(),"STALE_TARGET: target no longer exists; emit fresh AX");
        require(n.isEnabled()&&n.isVisibleToUser(),"TARGET_OCCLUDED");
        return n;
    }
    static void captureImage(JSONObject result,long deadline)throws Exception{
        imageReads.incrementAndGet();long started=SystemClock.uptimeMillis(),at=started;Bitmap image=null;byte[] png=null;
        String provider="android.uiautomation";
        // Some Android builds dereference a null ScreenshotHardwareBuffer in
        // UiAutomation even while the display is on. screencap reads the same
        // primary display without that framework wrapper; it injects no input.
        try{image=ax.takeScreenshot();}catch(NullPointerException unavailable){}
        if(image==null){
            long remaining=deadline-SystemClock.uptimeMillis()-100;
            require(remaining>0,"OBSERVATION_FAILED: screenshot deadline exceeded");
            at=SystemClock.uptimeMillis();
            JSONObject capture=shell(new JSONObject().put("argv",new JSONArray(Arrays.asList("/system/bin/screencap","-p")))
                .put("timeout_ms",remaining).put("max_output_bytes",16*1024*1024));
            require("exited".equals(capture.optString("state"))&&capture.optInt("exit_code",-1)==0&&
                "complete".equals(capture.optString("cleanup"))&&!capture.optBoolean("stdout_truncated"),
                "OBSERVATION_FAILED: primary-display screencap failed");
            png=Base64.decode(capture.getString("stdout"),Base64.DEFAULT);
            image=BitmapFactory.decodeByteArray(png,0,png.length);provider="android.screencap";
        }
        require(image!=null,"OBSERVATION_FAILED: screenshot returned no decodable image");
        long encodeStart=SystemClock.uptimeMillis();
        try{
            // screencap already supplied a PNG; do not compress it a second time.
            if(png==null){
                ByteArrayOutputStream out=new ByteArrayOutputStream();
                require(image.compress(Bitmap.CompressFormat.PNG,100,out),"OBSERVATION_FAILED: PNG encoding failed");
                png=out.toByteArray();
            }
            width=image.getWidth();height=image.getHeight();
            result.put("width",width).put("height",height).put("png",Base64.encodeToString(png,Base64.NO_WRAP))
                .put("image_at",at).put("capture_backend",provider)
                .put("capture_ms",encodeStart-started).put("encode_ms",SystemClock.uptimeMillis()-encodeStart)
                .put("png_bytes",png.length).put("capture_total_ms",SystemClock.uptimeMillis()-started);
        }finally{image.recycle();}
    }
    static JSONObject observe(JSONObject q)throws Exception{
        long deadline=SystemClock.uptimeMillis()+q.optInt("timeout_ms",1500);
        if(!q.optBoolean("tree",true)){
            JSONObject d=displayMetadata();long at=SystemClock.uptimeMillis();
            JSONObject r=new JSONObject().put("nodes",new JSONArray()).put("revision",cached==null?"0":cached.getString("revision"))
                .put("width",d.getInt("width")).put("height",d.getInt("height")).put("rotation",d.getInt("rotation")).put("captured_at",at)
                .put("age_ms",0).put("source","screenshot").put("truncated",false).put("pending_invalidations",0);
            if(q.optBoolean("screenshot",false))captureImage(r,deadline);
            return r;
        }
        boolean scan=cached==null||q.optString("mode","latest").equals("refresh")||(deferredObservations&&events.get()!=consumed);
        long readStart=SystemClock.uptimeMillis();
        if(scan){synchronized(gate){if(q.optBoolean("clear_framework_cache",false))require(ax.clearCache(),"ACTION_REJECTED");refresh();}}
        JSONObject r=new JSONObject(cached.toString());
        r.put("age_ms",Math.max(0,SystemClock.uptimeMillis()-r.getLong("captured_at")));
        r.put("pending_invalidations",Math.max(0,events.get()-consumed)).put("source",scan?"scan":"cache");
        r.put("ax_read_ms",SystemClock.uptimeMillis()-readStart).put("scan_performed",scan).put("framework_cache_cleared",scan&&q.optBoolean("clear_framework_cache",false));
        if(q.optBoolean("screenshot",false))captureImage(r,deadline);
        return r;
    }
    static void touch(float x,float y,boolean down,long since)throws Exception{
        MotionEvent e=MotionEvent.obtain(since,SystemClock.uptimeMillis(),down?MotionEvent.ACTION_DOWN:MotionEvent.ACTION_UP,x,y,0);
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        try{require(ax.injectInputEvent(e,true),"ACTION_REJECTED");}finally{e.recycle();}
    }
    static JSONObject execute(JSONObject q)throws Exception{
        synchronized(gate){
            String actionId=q.getString("action_id");
            if(ledger.containsKey(actionId)){JSONObject prev=ledger.get(actionId);require(prev.getString("request").equals(q.toString()),"INVALID_ARGUMENT");return prev.getJSONObject("result");}
            require(!fenced,"SESSION_CLOSED");
            // A pending entry survives failed delivery inside this executor process.
            JSONObject pending=new JSONObject().put("execution","unknown").put("action_id",actionId);
            ledger.put(actionId,new JSONObject().put("request",q.toString()).put("result",pending));
            String op=q.getString("action"); boolean ok=false, inputStarted=false;
            try {
                if(q.has("frame_width")){
                    JSONObject display=displayMetadata();
                    require(q.getInt("frame_width")==display.getInt("width")&&q.getInt("frame_height")==display.getInt("height")&&q.optInt("frame_rotation",0)==display.getInt("rotation"),"STALE_TARGET: display geometry changed since capture");
                }
                if(op.equals("input_text")){
                    long deadline=SystemClock.uptimeMillis()+q.optInt("timeout_ms",5000);
                    String ime=commandText(Arrays.asList("settings","get","secure","default_input_method"),deadline);
                    require(ime.equals("com.android.adbkeyboard/.AdbIME")||ime.equals("com.android.adbkeyboard/com.android.adbkeyboard.AdbIME"),
                        "UNSUPPORTED_CAPABILITY: activate com.android.adbkeyboard/.AdbIME before input_text");
                    boolean hasTarget=q.has("node_id")||q.has("x");
                    if(q.has("node_id")){
                        AccessibilityNodeInfo n=target(q);boolean clickable=false;
                        for(AccessibilityNodeInfo.AccessibilityAction a:n.getActionList())if(a.getId()==AccessibilityNodeInfo.ACTION_CLICK)clickable=true;
                        require(clickable,"UNSUPPORTED_CAPABILITY: input_text target does not advertise click; use screenshot coordinates");
                        inputStarted=true;require(n.performAction(AccessibilityNodeInfo.ACTION_CLICK),"ACTION_REJECTED");
                    }else if(q.has("x")){
                        // Physical coordinates do not depend on AX scene revisions.
                        if(!q.optString("app_id","").isEmpty())refresh();
                        require(q.optString("app_id","").isEmpty()||q.getString("app_id").equals(cached.optString("foreground_app_id")),"TARGET_NOT_FOREGROUND");
                        float x=(float)q.getDouble("x"),y=(float)q.getDouble("y");
                        require(Float.isFinite(x)&&Float.isFinite(y)&&x>=0&&y>=0&&x<width&&y<height,"INVALID_ARGUMENT: coordinates outside the display");
                        long down=SystemClock.uptimeMillis();inputStarted=true;inputReleased=false;
                        try{touch(x,y,true,down);}finally{touch(x,y,false,down);inputReleased=true;}
                    }else if(!q.optString("app_id","").isEmpty()){
                        refresh();require(q.getString("app_id").equals(cached.optString("foreground_app_id")),"TARGET_NOT_FOREGROUND");
                    }
                    if(hasTarget)SystemClock.sleep(500);
                    String encoded=Base64.encodeToString(q.getString("text").getBytes("UTF-8"),Base64.NO_WRAP);
                    inputStarted=true;
                    String delivery=commandText(Arrays.asList("am","broadcast","--user","current","-a","ADB_INPUT_B64","-p","com.android.adbkeyboard","--es","msg",encoded),deadline);
                    require(delivery.contains("Broadcast completed"),"ACTION_OUTCOME_UNKNOWN: ADBKeyboard broadcast delivery unconfirmed");
                    ok=true;
                }else if(op.equals("click")||op.equals("perform")||op.equals("scroll")||op.equals("set_text")||op.equals("select_text")){
                    AccessibilityNodeInfo n=target(q);Bundle args=new Bundle();int action=AccessibilityNodeInfo.ACTION_CLICK;
                    if(op.equals("perform"))action=Integer.parseInt(q.getString("node_action"));
                    if(op.equals("set_text")){action=AccessibilityNodeInfo.ACTION_SET_TEXT;args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,q.getString("text"));}
                    if(op.equals("select_text")){action=AccessibilityNodeInfo.ACTION_SET_SELECTION;args.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT,q.getInt("start"));args.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT,q.getInt("end"));}
                    if(op.equals("scroll")){String d=q.getString("direction");action=(d.equals("down")||d.equals("right"))?AccessibilityNodeInfo.ACTION_SCROLL_FORWARD:AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;}
                    boolean advertised=false;for(AccessibilityNodeInfo.AccessibilityAction a:n.getActionList())if(a.getId()==action)advertised=true;
                    require(advertised,"UNSUPPORTED_CAPABILITY: target does not advertise action "+action+". Choose a node with that action, or use screenshot coordinates for a physical click");ok=n.performAction(action,args);
                }else if(op.equals("point_click")||op.equals("point_scroll")||op.equals("point_long_press")||op.equals("node_scroll")||op.equals("drag")){
                    long gestureDeadline=SystemClock.uptimeMillis()+q.optInt("timeout_ms",3000);
                    if(op.equals("node_scroll")){
                        AccessibilityNodeInfo n=target(q);Rect bounds=new Rect();n.getBoundsInScreen(bounds);
                        require(bounds.intersect(0,0,width,height)&&bounds.width()>1&&bounds.height()>1,"TARGET_OCCLUDED");
                        String direction=q.getString("direction");double amount=q.optDouble("amount",1);
                        require(Arrays.asList("up","down","left","right").contains(direction)&&Double.isFinite(amount)&&amount>0&&amount<=1,"INVALID_ARGUMENT");
                        boolean horizontal=direction.equals("left")||direction.equals("right");
                        float cx=(bounds.left+bounds.right-1)/2f,cy=(bounds.top+bounds.bottom-1)/2f;
                        float half=(float)(.4*amount*(horizontal?bounds.width()-1:bounds.height()-1));
                        int sign=(direction.equals("down")||direction.equals("right"))?1:-1;
                        q.put("x",horizontal?cx+half*sign:cx).put("y",horizontal?cy:cy+half*sign)
                            .put("end_x",horizontal?cx-half*sign:cx).put("end_y",horizontal?cy:cy-half*sign);
                    }
                    // Physical coordinates do not depend on AX scene revisions.
                    if(!q.optString("app_id","").isEmpty())refresh();
                    require(q.optString("app_id","").isEmpty()||q.getString("app_id").equals(cached.optString("foreground_app_id")),"TARGET_NOT_FOREGROUND");
                    float x=(float)q.getDouble("x"),y=(float)q.getDouble("y");
                    boolean drag=op.equals("drag");
                    boolean swipe=op.equals("point_scroll")||op.equals("node_scroll")||drag;
                    float ex=swipe?(float)q.getDouble("end_x"):x,ey=swipe?(float)q.getDouble("end_y"):y;
                    boolean hold=op.equals("point_long_press");
                    int duration=(swipe||hold)?q.optInt("duration_ms",500):0;
                    int holdMs=drag?(q.isNull("hold_ms")?android.view.ViewConfiguration.getLongPressTimeout()+100:q.getInt("hold_ms")):0;
                    require(Float.isFinite(x)&&Float.isFinite(y)&&Float.isFinite(ex)&&Float.isFinite(ey)&&x>=0&&y>=0&&x<width&&y<height&&ex>=0&&ey>=0&&ex<width&&ey<height,"INVALID_ARGUMENT: coordinates outside the display");
                    require(!(swipe||hold)||(duration>=50&&duration<=5000&&duration<q.optInt("timeout_ms",3000)),"INVALID_ARGUMENT: invalid gesture duration");
                    require(holdMs>=0&&holdMs<=5000&&(!drag||(long)holdMs+duration+100<gestureDeadline-SystemClock.uptimeMillis()),"INVALID_ARGUMENT: drag hold and movement must fit the remaining budget");
                    long t=SystemClock.uptimeMillis();inputReleased=false;
                    float lastX=x,lastY=y;
                    try{
                        inputStarted=true;touch(x,y,true,t);
                        if(drag){
                            long until=t+holdMs;
                            while(SystemClock.uptimeMillis()<until){
                                require(SystemClock.uptimeMillis()+100<gestureDeadline,"ACTION_OUTCOME_UNKNOWN: drag deadline exceeded");
                                SystemClock.sleep(Math.max(0,Math.min(16,until-SystemClock.uptimeMillis())));
                            }
                        }
                        if(swipe){
                            int steps=Math.max(2,duration/16);
                            for(int i=1;i<=steps;i++){
                                long targetTime=t+holdMs+(long)duration*i/steps;
                                long delay=targetTime-SystemClock.uptimeMillis();if(delay>0)SystemClock.sleep(delay);
                                if(drag)require(SystemClock.uptimeMillis()+100<gestureDeadline,"ACTION_OUTCOME_UNKNOWN: drag deadline exceeded");
                                float f=(float)i/steps;
                                float nextX=x+(ex-x)*f,nextY=y+(ey-y)*f;
                                MotionEvent move=MotionEvent.obtain(t,SystemClock.uptimeMillis(),MotionEvent.ACTION_MOVE,nextX,nextY,0);
                                move.setSource(InputDevice.SOURCE_TOUCHSCREEN);
                                try{require(ax.injectInputEvent(move,true),"ACTION_REJECTED");lastX=nextX;lastY=nextY;}finally{move.recycle();}
                            }
                        }
                        if(hold)SystemClock.sleep(duration);
                        touch(ex,ey,false,t);inputReleased=true;ok=true;
                    }finally{
                        if(!inputReleased){MotionEvent cancel=MotionEvent.obtain(t,SystemClock.uptimeMillis(),MotionEvent.ACTION_CANCEL,lastX,lastY,0);cancel.setSource(InputDevice.SOURCE_TOUCHSCREEN);try{inputReleased=ax.injectInputEvent(cancel,true);}finally{cancel.recycle();if(!inputReleased)fenced=true;}}
                    }
                }else if(op.equals("launch")){
                    String pkg=q.getString("app_id").substring(4);
                    JSONObject lookup=shell(new JSONObject().put("argv",new JSONArray(Arrays.asList("cmd","package","resolve-activity","--brief","-a","android.intent.action.MAIN","-c","android.intent.category.LAUNCHER","-p",pkg))).put("timeout_ms",q.optInt("timeout_ms",3000)));
                    String[] lines=new String(Base64.decode(lookup.getString("stdout"),Base64.DEFAULT),"UTF-8").trim().split("\\n");
                    String component=lines[lines.length-1];require(component.startsWith(pkg+"/"),"TARGET_NOT_FOUND");
                    JSONObject cmd=new JSONObject().put("argv",new JSONArray(Arrays.asList("am","start","-W","-n",component))).put("timeout_ms",q.optInt("timeout_ms",3000));
                    JSONObject r=shell(cmd);String report=new String(Base64.decode(r.getString("stdout"),Base64.DEFAULT),"UTF-8");ok=r.optInt("exit_code",-1)==0&&report.contains("Status: ok");
                }else if(op.equals("system")||op.equals("press_key")){
                    if(op.equals("press_key")&&!q.optString("app_id","").isEmpty()){refresh();require(q.getString("app_id").equals(cached.optString("foreground_app_id")),"TARGET_NOT_FOREGROUND");}
                    String key=q.optString("button","");int code=op.equals("press_key")?q.getInt("key_code"):key.equals("back")?KeyEvent.KEYCODE_BACK:key.equals("home")?KeyEvent.KEYCODE_HOME:key.equals("recents")?KeyEvent.KEYCODE_APP_SWITCH:0;
                    require(code!=0,"UNSUPPORTED_CAPABILITY");long downTime=SystemClock.uptimeMillis();inputReleased=false;try{inputStarted=true;ok=ax.injectInputEvent(new KeyEvent(downTime,downTime,KeyEvent.ACTION_DOWN,code,0,0,KeyCharacterMap.VIRTUAL_KEYBOARD,0,0,InputDevice.SOURCE_KEYBOARD),true);}finally{inputReleased=ax.injectInputEvent(new KeyEvent(downTime,SystemClock.uptimeMillis(),KeyEvent.ACTION_UP,code,0,0,KeyCharacterMap.VIRTUAL_KEYBOARD,0,0,InputDevice.SOURCE_KEYBOARD),true);}ok=ok&&inputReleased;
                }else throw new Exception("UNSUPPORTED_CAPABILITY");
                require(ok,"ACTION_REJECTED");events.incrementAndGet();
                JSONObject r=new JSONObject().put("execution","completed").put("action_id",actionId).put("effect","unknown").put("at",SystemClock.uptimeMillis());
                ledger.get(actionId).put("result",r);return r;
            }catch(Exception e){if(inputStarted){if(op.equals("drag"))fenced=true;e=new Exception("ACTION_OUTCOME_UNKNOWN: input dispatch failed; do not replay. "+e.getMessage(),e);}pending.put("error",e.getMessage());String code=e.getMessage().split(":",2)[0];if(Arrays.asList("STALE_TARGET","INVALID_ARGUMENT","UNSUPPORTED_CAPABILITY","TARGET_NOT_FOREGROUND","TARGET_OCCLUDED","ACTION_REJECTED").contains(code))pending.put("execution","rejected").put("effect","not_applied");throw e;}
        }
    }
    static String commandText(List<String> argv,long deadline)throws Exception{
        long remaining=deadline-SystemClock.uptimeMillis();require(remaining>0,"ACTION_OUTCOME_UNKNOWN: input_text deadline exceeded");
        JSONObject r=shell(new JSONObject().put("argv",new JSONArray(argv)).put("timeout_ms",remaining));
        require("exited".equals(r.optString("state"))&&r.optInt("exit_code",-1)==0&&"complete".equals(r.optString("cleanup")),
            "ACTION_OUTCOME_UNKNOWN: input_text command failed or timed out");
        return new String(Base64.decode(r.getString("stdout"),Base64.DEFAULT),"UTF-8").trim();
    }
    static class Collector extends Thread{
        final InputStream in;final ByteArrayOutputStream out=new ByteArrayOutputStream();final int cap;volatile boolean truncated=false;
        Collector(InputStream i,int c){in=i;cap=c;setDaemon(true);}
        public void run(){try{byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1){synchronized(out){int take=Math.max(0,Math.min(n,cap-out.size()));out.write(b,0,take);if(take<n)truncated=true;}}}catch(Exception ignored){}}
        String value(){synchronized(out){return Base64.encodeToString(out.toByteArray(),Base64.NO_WRAP);}}
    }
    static JSONObject shell(JSONObject q)throws Exception{
        long start=SystemClock.uptimeMillis();List<String> argv=new ArrayList<>();
        if(q.has("argv")){JSONArray a=q.getJSONArray("argv");for(int i=0;i<a.length();i++)argv.add(a.getString(i));}
        else {argv.add(q.optString("interpreter","/system/bin/sh"));argv.add("-c");argv.add(q.getString("script"));}
        require(!argv.isEmpty(),"INVALID_ARGUMENT");ProcessBuilder b=new ProcessBuilder(argv);
        if(q.has("cwd"))b.directory(new File(q.getString("cwd")));
        if(q.has("env")){JSONObject env=q.getJSONObject("env");Iterator<String> keys=env.keys();while(keys.hasNext()){String k=keys.next();if(env.isNull(k))b.environment().remove(k);else b.environment().put(k,env.getString(k));}}
        java.lang.Process p=b.start();int cap=q.optInt("max_output_bytes",1048576);Collector stdout=new Collector(p.getInputStream(),cap),stderr=new Collector(p.getErrorStream(),cap);stdout.start();stderr.start();
        Thread writer=new Thread(()->{try{if(q.has("stdin"))p.getOutputStream().write(Base64.decode(q.optString("stdin"),Base64.DEFAULT));p.getOutputStream().close();}catch(Exception ignored){}},"mai-ui-stdin");writer.setDaemon(true);writer.start();
        boolean exited=p.waitFor(q.optInt("timeout_ms",30000),TimeUnit.MILLISECONDS);
        if(!exited){p.destroyForcibly();p.waitFor(1000,TimeUnit.MILLISECONDS);}
        stdout.join(1000);stderr.join(1000);writer.join(1000);
        boolean complete=!p.isAlive()&&!stdout.isAlive()&&!stderr.isAlive()&&!writer.isAlive();
        return new JSONObject().put("state",exited?"exited":"timed_out").put("exit_code",p.isAlive()?JSONObject.NULL:p.exitValue())
            .put("stdout",stdout.value()).put("stderr",stderr.value()).put("stdout_truncated",stdout.truncated).put("stderr_truncated",stderr.truncated)
            .put("duration_ms",SystemClock.uptimeMillis()-start).put("cleanup",complete?"complete":"unknown");
    }
    static JSONObject listApps(JSONObject q)throws Exception{
        require(!fenced,"SESSION_CLOSED");
        // app_process does not prepare the framework main looper for us.
        if(Looper.getMainLooper()==null)Looper.prepareMainLooper();
        else if(Looper.myLooper()==null)Looper.prepare();
        Class<?> activityThread=Class.forName("android.app.ActivityThread");
        Object thread=activityThread.getMethod("currentActivityThread").invoke(null);
        if(thread==null)thread=activityThread.getMethod("systemMain").invoke(null);
        Context context=(Context)activityThread.getMethod("getSystemContext").invoke(thread);
        int userId=(Integer)Class.forName("android.app.ActivityManager").getMethod("getCurrentUser").invoke(null);
        UserHandle user=(UserHandle)UserHandle.class.getMethod("of",int.class).invoke(null,userId);
        context=(Context)Context.class.getMethod("createContextAsUser",UserHandle.class,int.class).invoke(context,user,0);
        PackageManager pm=context.getPackageManager();
        Intent main=new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        Set<String> launchable=new HashSet<>();
        for(ResolveInfo resolved:pm.queryIntentActivities(main,0)){
            if(resolved.activityInfo!=null && resolved.activityInfo.enabled && resolved.activityInfo.exported && resolved.activityInfo.applicationInfo.enabled)launchable.add(resolved.activityInfo.packageName);
        }
        List<ApplicationInfo> installed=pm.getInstalledApplications(0);
        installed.sort(Comparator.comparing(a->a.packageName));
        JSONArray apps=new JSONArray();boolean complete=true;
        for(ApplicationInfo app:installed){
            boolean canLaunch=launchable.contains(app.packageName);
            if(q.optBoolean("launchable_only",true)&&!canLaunch)continue;
            try{
                String label=str(pm.getApplicationLabel(app));if(label.isEmpty())label=app.packageName;
                apps.put(new JSONObject().put("id","app:"+app.packageName).put("native_id",app.packageName).put("label",label).put("launchable",canLaunch));
            }catch(RuntimeException changedDuringRead){complete=false;}
        }
        return new JSONObject().put("apps",apps).put("complete",complete);
    }
    static JSONObject displayMetadata()throws Exception{
        Class<?> manager=Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object global=manager.getMethod("getInstance").invoke(null);
        Display display=(Display)manager.getMethod("getRealDisplay",int.class).invoke(global,Display.DEFAULT_DISPLAY);
        require(display!=null,"TARGET_NOT_FOUND: primary display unavailable");
        android.util.DisplayMetrics metrics=new android.util.DisplayMetrics();display.getRealMetrics(metrics);
        width=metrics.widthPixels;height=metrics.heightPixels;
        return new JSONObject().put("id","display:0").put("generation",width+"x"+height)
            .put("width",width).put("height",height).put("rotation",display.getRotation()*90)
            .put("scale_factor",1).put("insets",JSONObject.NULL);
    }
    static JSONObject fileStat(Path path)throws Exception{
        BasicFileAttributes a=Files.readAttributes(path,BasicFileAttributes.class,LinkOption.NOFOLLOW_LINKS);
        return new JSONObject().put("path",path.toString()).put("size",a.size()).put("kind",a.isSymbolicLink()?"symlink":a.isDirectory()?"directory":a.isRegularFile()?"file":"other");
    }
    static JSONObject file(JSONObject q)throws Exception{
        Path path=Paths.get(q.getString("path"));require(path.isAbsolute(),"INVALID_ARGUMENT");
        String op=q.getString("operation");boolean writing=Arrays.asList("write","mkdir","remove","move").contains(op);
        try{
            if(op.equals("stat"))return fileStat(path);
            if(op.equals("list")){
                JSONArray entries=new JSONArray();boolean complete=true;
                try(DirectoryStream<Path> stream=Files.newDirectoryStream(path)){
                    for(Path child:stream){if(entries.length()>=1000){complete=false;break;}entries.put(fileStat(child));}
                }
                return new JSONObject().put("entries",entries).put("complete",complete);
            }
            if(op.equals("read")){
                int cap=q.optInt("max_bytes",1048576);require(cap>=0&&cap<=1048576,"INVALID_ARGUMENT");
                require(Files.isRegularFile(path),"INVALID_ARGUMENT: expected regular file");
                ByteArrayOutputStream out=new ByteArrayOutputStream();
                try(InputStream in=Files.newInputStream(path)){byte[] buffer=new byte[8192];int n;while((n=in.read(buffer))>=0){require(out.size()+n<=cap,"FILE_TOO_LARGE");out.write(buffer,0,n);}}
                return new JSONObject().put("data",Base64.encodeToString(out.toByteArray(),Base64.NO_WRAP));
            }
            if(op.equals("write")){
                byte[] data=Base64.decode(q.getString("data"),Base64.DEFAULT);require(data.length<=1048576,"INVALID_ARGUMENT");
                // CREATE_NEW atomically rejects existing paths. Failed writes may leave
                // a partial new file and are reported as unknown, never retried.
                if(!q.optBoolean("overwrite",false))Files.write(path,data,StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE);
                else {
                    Path temp=Files.createTempFile(path.getParent(),".mai-ui-", ".tmp");
                    try{Files.write(temp,data);Files.move(temp,path,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}
                    finally{Files.deleteIfExists(temp);}
                }
                return new JSONObject().put("bytes",data.length).put("path",path.toString());
            }
            if(op.equals("mkdir")){if(q.optBoolean("parents",false))Files.createDirectories(path);else Files.createDirectory(path);}
            else if(op.equals("remove")){
                if(q.optBoolean("recursive",false)&&Files.isDirectory(path,LinkOption.NOFOLLOW_LINKS)){
                    Files.walkFileTree(path,new SimpleFileVisitor<Path>(){
                        public FileVisitResult visitFile(Path p,BasicFileAttributes attrs)throws IOException{Files.delete(p);return FileVisitResult.CONTINUE;}
                        public FileVisitResult postVisitDirectory(Path p,IOException error)throws IOException{if(error!=null)throw error;Files.delete(p);return FileVisitResult.CONTINUE;}
                    });
                }else Files.delete(path);
            }else if(op.equals("move")){
                Path destination=Paths.get(q.getString("destination"));require(destination.isAbsolute(),"INVALID_ARGUMENT");
                if(q.optBoolean("overwrite",false))Files.move(path,destination,StandardCopyOption.REPLACE_EXISTING);else Files.move(path,destination);
            }else throw new Exception("UNSUPPORTED_CAPABILITY");
            return new JSONObject().put("path",path.toString()).put("completed",true);
        }catch(FileAlreadyExistsException e){throw new Exception("ALREADY_EXISTS: "+path);}
        catch(NoSuchFileException e){throw new Exception("TARGET_NOT_FOUND: "+path);}
        catch(AccessDeniedException e){throw new Exception("PERMISSION_DENIED: "+path);}
        catch(IOException e){throw new Exception((writing?"ACTION_OUTCOME_UNKNOWN":"FILE_READ_FAILED")+": "+e.getMessage());}
    }
    static JSONObject dispatch(JSONObject q)throws Exception{
        String op=q.getString("op");
        if(op.equals("describe")){
            JSONObject capabilities=new JSONObject();for(String k:Arrays.asList("ui.observe.tree","ui.observe.screenshot","ui.click.primary","ui.input_text","ui.perform","ui.scroll.steps","ui.scroll.coordinates","ui.click.coordinates","device.list_apps","device.list_displays","app.launch","android.press","shell.run"))capabilities.put(k,new JSONObject().put("state","ready").put("provider","android.uiautomation"));
            for(String k:Arrays.asList("ui.set_text","ui.select_text","ui.press_key","ui.long_press","ui.scroll.viewport","ui.drag","files.read","files.write","files.stat","files.list","files.mkdir","files.remove","files.move"))capabilities.put(k,new JSONObject().put("state","ready").put("provider","android.uiautomation"));
            capabilities.getJSONObject("ui.drag").put("default_hold_ms",android.view.ViewConfiguration.getLongPressTimeout()+100).put("pointers",1);
            capabilities.put("ui.wait_for_stabilize",new JSONObject().put("state","ready").put("provider","android.uiautomation").put("strategy","fixed_delay").put("delay_ms",2000));
            try{
                String ime=commandText(Arrays.asList("settings","get","secure","default_input_method"),SystemClock.uptimeMillis()+1500);
                if(!ime.equals("com.android.adbkeyboard/.AdbIME")&&!ime.equals("com.android.adbkeyboard/com.android.adbkeyboard.AdbIME"))
                    capabilities.getJSONObject("ui.input_text").put("state","unavailable").put("reason","ADBKeyboard must be installed and selected");
            }catch(Exception e){capabilities.getJSONObject("ui.input_text").put("state","unavailable").put("reason","Could not verify active input method");}
            int uid=android.os.Process.myUid();JSONObject identity=new JSONObject().put("platform","android").put("principal",uid==0?"root":uid==2000?"shell":Integer.toString(uid)).put("uid",uid)
                .put("gid",android.system.Os.getgid());
            JSONObject provider=new JSONObject().put("id","device-shell").put("kind","adb").put("state","ready").put("identity",identity).put("interpreters",new JSONArray(Arrays.asList("/system/bin/sh"))).put("default_interpreter","/system/bin/sh").put("default_cwd","/").put("argv",true).put("streaming",false).put("stdin",true).put("pty",false).put("cancellation","process_only").put("reason","Only the managed child is reaped; descendants are not guaranteed.");
            return new JSONObject().put("protocol_major",1).put("sdk_features_version","0.2").put("platform","android").put("os_version",Build.VERSION.RELEASE).put("capabilities",capabilities).put("shell_providers",new JSONArray().put(provider))
                .put("execution_security",new JSONObject().put("no_new_privileges","1".equals(processCredentials().get("NoNewPrivs"))))
                .put("observation_stats",new JSONObject().put("tree_reads",treeReads.get()).put("image_reads",imageReads.get()).put("deferred",deferredObservations));
        }
        if(op.equals("displays"))return new JSONObject().put("displays",new JSONArray().put(displayMetadata()));
        if(op.equals("observe"))return observe(q);
        if(op.equals("wait_for_stabilize")){
            require(!fenced,"SESSION_CLOSED");
            require(q.optInt("timeout_ms",5000)>=2000,"INVALID_ARGUMENT: Android stabilization wait needs at least 2000 ms remaining");
            // Placeholder adapter policy: no observation, image comparison, or input.
            Thread.sleep(2000);
            return new JSONObject().put("strategy","fixed_delay").put("waited_ms",2000);
        }
        if(op.equals("list_apps"))return listApps(q);
        if(op.equals("file")){require(!fenced,"SESSION_CLOSED");return file(q);}
        if(op.equals("action"))return execute(q);
        if(op.equals("shell")){require(!fenced,"SESSION_CLOSED");events.incrementAndGet();try{return shell(q);}finally{events.incrementAndGet();}}
        if(op.equals("status")){JSONObject row=ledger.get(q.getString("action_id"));return row==null?new JSONObject().put("execution","unknown"):row.getJSONObject("result");}
        if(op.equals("stop")){synchronized(gate){fenced=true;return new JSONObject().put("stopped",true).put("input_released",inputReleased);}}
        throw new Exception("UNSUPPORTED_CAPABILITY");
    }
    public static void main(String[] args)throws Exception{
        if("1".equals(System.getenv("MAI_UI_ANDROID_REQUIRE_SHELL")))verifyShellCredentials();
        // app_process has no application looper; newer AccessibilityInteractionClient
        // constructors require a main looper even when UiAutomation uses callbacks.
        if(Looper.getMainLooper()==null)Looper.prepareMainLooper();
        HandlerThread callbacks=new HandlerThread("mai-ui-ax-events");callbacks.start();
        Class<?> api=Class.forName("android.app.IUiAutomationConnection");Object c=Class.forName("android.app.UiAutomationConnection").getDeclaredConstructor().newInstance();
        ax=UiAutomation.class.getConstructor(Looper.class,api).newInstance(callbacks.getLooper(),c);
        UiAutomation.class.getMethod("connect",int.class).invoke(ax,UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
        AccessibilityServiceInfo info=ax.getServiceInfo();info.flags|=AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS|AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;ax.setServiceInfo(info);
        try{sourceId=AccessibilityNodeInfo.class.getMethod("getSourceNodeId");}catch(Exception ignored){}
        ax.setOnAccessibilityEventListener(e->{
            events.incrementAndGet();
            if(e.getEventType()==AccessibilityEvent.TYPE_WINDOWS_CHANGED &&
               (e.getWindowChanges() & AccessibilityEvent.WINDOWS_CHANGE_REMOVED)!=0 && e.getWindowId()==activeWindow)
                lostWindows.incrementAndGet();
        });
        if(deferredObservations){
            // Display metadata needs no screenshot or AX traversal.
            Class<?> manager=Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object global=manager.getMethod("getInstance").invoke(null);
            Display display=(Display)manager.getMethod("getRealDisplay",int.class).invoke(global,Display.DEFAULT_DISPLAY);
            android.util.DisplayMetrics metrics=new android.util.DisplayMetrics();display.getRealMetrics(metrics);
            width=metrics.widthPixels;height=metrics.heightPixels;
        }else{
            imageReads.incrementAndGet();Bitmap first=ax.takeScreenshot();if(first!=null){width=first.getWidth();height=first.getHeight();first.recycle();}
            refresh();
            Thread updater=new Thread(()->{while(true){try{Thread.sleep(100);if(events.get()!=consumed)refresh();}catch(Exception ignored){}}},"mai-ui-ax-cache");updater.setDaemon(true);updater.start();
        }
        BufferedReader input=new BufferedReader(new InputStreamReader(System.in,"UTF-8"));String line;
        try{while((line=input.readLine())!=null){JSONObject reply=new JSONObject();try{JSONObject q=new JSONObject(line);reply.put("result",dispatch(q));}catch(Exception e){reply.put("error",e.getMessage()==null?e.toString():e.getMessage());}System.out.println(reply.toString());System.out.flush();}}
        finally{UiAutomation.class.getMethod("disconnect").invoke(ax);callbacks.quitSafely();}
        System.exit(0);
    }
}
