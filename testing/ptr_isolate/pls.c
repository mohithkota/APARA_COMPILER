int g[3]={1,2,3}; long long results[4];
int main(){
    int loc[3]; loc[0]=1; loc[1]=2; loc[2]=3;
    int *pg=g; int *pl=loc;
    *pg=99;          /* store into GLOBAL via ptr */
    *pl=88;          /* store into LOCAL via ptr  */
    pg[2]=77; pl[2]=66;
    results[0]=g[0];   /* 99 */
    results[1]=loc[0]; /* 88 */
    results[2]=g[2];   /* 77 */
    results[3]=loc[2]; /* 66 */
    return 1;
}
