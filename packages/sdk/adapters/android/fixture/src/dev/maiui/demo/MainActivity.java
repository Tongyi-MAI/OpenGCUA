package dev.maiui.demo;
import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.view.View;
import android.widget.*;
import org.json.JSONObject;
import java.io.*;

/** Dedicated demo fixture. Its private JSON file is an independent app-state oracle. */
public class MainActivity extends Activity {
    int saves=0; EditText title,body; TextView status,details;
    int dp(int n){return (int)(getResources().getDisplayMetrics().density*n);}
    TextView label(String s,int size){TextView t=new TextView(this);t.setText(s);t.setTextSize(size);t.setTextColor(Color.rgb(225,233,243));return t;}
    @Override public void onCreate(Bundle b){super.onCreate(b);
        if(getIntent().getBooleanExtra("drag_fixture",false)){showDragFixture();return;}
        if(getIntent().getBooleanExtra("rejection_fixture",false)){showRejectionFixture();return;}
        LinearLayout root=new LinearLayout(this);root.setOrientation(1);root.setPadding(dp(24),dp(28),dp(24),dp(20));root.setBackgroundColor(Color.rgb(13,19,30));
        root.addView(label("mai-ui / Notes",26));root.addView(label("A real Android app. Controlled with Python.",14));
        title=new EditText(this);title.setSingleLine(true);title.setHint("Untitled note");title.setContentDescription("Title");title.setTextColor(Color.WHITE);title.setHintTextColor(Color.GRAY);root.addView(title,new LinearLayout.LayoutParams(-1,dp(68)));
        body=new EditText(this);body.setHint("Write something");body.setContentDescription("Body");body.setTextColor(Color.WHITE);body.setHintTextColor(Color.GRAY);body.setGravity(48);root.addView(body,new LinearLayout.LayoutParams(-1,dp(150)));
        Button save=new Button(this);save.setText("Save note");root.addView(save);
        status=label("Ready",18);status.setContentDescription("Save status");root.addView(status);
        Button toggle=new Button(this);toggle.setText("Show details");root.addView(toggle);
        toggle.setOnClickListener(v->{
            if(details==null){details=label("Changes appear in the AX diff.",16);details.setContentDescription("Note details");root.addView(details,root.indexOfChild(toggle));toggle.setText("Hide details");}
            else {root.removeView(details);details=null;toggle.setText("Show details");}
        });
        for(int i=0;i<2;i++){Button more=new Button(this);more.setText("More");root.addView(more);}
        save.setOnClickListener(v->{try{saves++;JSONObject j=new JSONObject().put("title",title.getText().toString()).put("body",body.getText().toString()).put("saves",saves);try(FileOutputStream out=openFileOutput("state.json",MODE_PRIVATE)){out.write(j.toString().getBytes("UTF-8"));}status.setText("Saved · "+saves);}catch(Exception e){status.setText("Save failed");}});
        setContentView(root);
    }
    void showRejectionFixture(){
        LinearLayout root=new LinearLayout(this);root.setOrientation(1);
        Button reject=new Button(this);reject.setText("Reject native click");
        reject.setAccessibilityDelegate(new View.AccessibilityDelegate(){
            @Override public boolean performAccessibilityAction(View view,int action,Bundle args){
                if(action==android.view.accessibility.AccessibilityNodeInfo.ACTION_CLICK)return false;
                return super.performAccessibilityAction(view,action,args);
            }
        });
        root.addView(reject);
        Button save=new Button(this);save.setText("Save after rejection");root.addView(save);
        save.setOnClickListener(v->{try{
            JSONObject state=new JSONObject().put("saves",++saves);
            try(FileOutputStream out=openFileOutput("rejection-state.json",MODE_PRIVATE)){out.write(state.toString().getBytes("UTF-8"));}
        }catch(Exception e){throw new RuntimeException(e);}});
        setContentView(root);
    }
    void showDragFixture(){
        LinearLayout root=new LinearLayout(this);root.setOrientation(1);
        root.addView(label("Immediate drag",20));
        root.addView(new DragPad(false),new LinearLayout.LayoutParams(-1,dp(200)));
        root.addView(label("Long press then drag",20));
        root.addView(new DragPad(true),new LinearLayout.LayoutParams(-1,dp(200)));
        setContentView(root);
    }
    class DragPad extends View {
        final boolean hold; boolean acquired=false,pressed=false,moved=false;float px=30,py=50,downX=0,downY=0;long firstMoveMs=-1;
        int downs=0,moves=0,ups=0,cancels=0,drops=0;long downTime=0;boolean continuous=true;
        DragPad(boolean hold){super(MainActivity.this);this.hold=hold;setContentDescription(hold?"Hold drag pad":"Immediate drag pad");setClickable(true);setLongClickable(true);setOnLongClickListener(v->{if(moved||!pressed)return false;acquired=true;record();return true;});}
        @Override protected void onDraw(android.graphics.Canvas canvas){super.onDraw(canvas);canvas.drawColor(Color.DKGRAY);android.graphics.Paint p=new android.graphics.Paint();p.setColor(Color.GREEN);canvas.drawRect(getWidth()/2f,0,getWidth(),getHeight(),p);p.setColor(Color.RED);canvas.drawCircle(px,py,dp(15),p);}
        @Override public boolean onTouchEvent(android.view.MotionEvent e){
            super.onTouchEvent(e);
            int a=e.getActionMasked();
            if(a==android.view.MotionEvent.ACTION_DOWN){downs++;pressed=true;acquired=!hold;downTime=e.getDownTime();downX=e.getX();downY=e.getY();moved=false;firstMoveMs=-1;}
            else continuous&=e.getDownTime()==downTime&&e.getPointerId(0)==0;
            if(a==android.view.MotionEvent.ACTION_MOVE){moves++;if(firstMoveMs<0)firstMoveMs=e.getEventTime()-downTime;if(!acquired&&Math.hypot(e.getX()-downX,e.getY()-downY)>android.view.ViewConfiguration.get(MainActivity.this).getScaledTouchSlop()){moved=true;cancelLongPress();}if(acquired){px=e.getX();py=e.getY();}}
            if(a==android.view.MotionEvent.ACTION_UP){ups++;pressed=false;if(acquired){px=e.getX();py=e.getY();if(e.getX()>getWidth()/2f)drops++;}}
            if(a==android.view.MotionEvent.ACTION_CANCEL){cancels++;pressed=false;acquired=false;}
            record();invalidate();return true;
        }
        void record(){try{
            JSONObject state=new JSONObject().put("downs",downs).put("moves",moves).put("ups",ups).put("cancels",cancels).put("drops",drops).put("pressed",pressed).put("acquired",acquired).put("continuous",continuous).put("first_move_ms",firstMoveMs).put("x",px).put("y",py);
            try(FileOutputStream out=openFileOutput(hold?"hold-drag.json":"immediate-drag.json",MODE_PRIVATE)){out.write(state.toString().getBytes("UTF-8"));}
        }catch(Exception error){throw new RuntimeException(error);}}
    }
}
