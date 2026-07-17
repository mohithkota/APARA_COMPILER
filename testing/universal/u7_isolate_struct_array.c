struct Pt{int x;int y;}; struct Pt pts[3]={{1,2},{3,4},{5,6}}; long long results[8];
int main(){
    results[0]=pts[0].x;      /* 1  */
    results[1]=pts[0].y;      /* 2  */
    results[2]=pts[1].x;      /* 3  */
    results[3]=pts[1].y;      /* 4  */
    results[4]=pts[2].x;      /* 5  */
    struct Pt *p=&pts[1];
    results[5]=p->x;          /* 3  */
    results[6]=p->y;          /* 4  */
    results[7]=(p+1)->x;      /* 5  */
    return 1;
}
