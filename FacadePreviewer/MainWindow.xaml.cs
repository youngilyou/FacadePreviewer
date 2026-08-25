using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Navigation;
using System.Windows.Shapes;

using FacadePreviewer.Services;
using FacadePreviewer.ViewModels;

namespace FacadePreviewer;

/// <summary>
/// Interaction logic for MainWindow.xaml
/// </summary>
public partial class MainWindow : Window
{

    // Result-image pan state (OnResultImageMouseDown/Move/Up below) -- zoom state lives directly
    // on ResultImageScale, no separate field needed for that.
    private bool _isPanningResultImage;
    private Point _panMouseStart;
    private double _panStartX;
    private double _panStartY;
    private const double MinResultImageZoom = 0.2;
    private const double MaxResultImageZoom = 10.0;

    // Same pan-state shape as the result-image viewer above, kept separate since both viewers can
    // exist in the same window (never actually visible at the same time, see MainWindow.xaml's
    // IsShowingSelectedFrame/HasScanResult DataTriggers, but each needs its own independent
    // zoom/pan state regardless).
    private bool _isPanningSelectedFrame;
    private Point _selectedFramePanMouseStart;
    private double _selectedFramePanStartX;
    private double _selectedFramePanStartY;

    public MainWindow()
    {
        InitializeComponent();
        Closing += OnClosing;
        SourceInitialized += (_, _) => DarkTitleBar.Apply(this);
    }

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        // Stops the native DDS subscriber thread cleanly -- without this the
        // process can hang on exit while FacadeDdsBridge.dll's teardown
        // thread waits out its bounded timeout (see DdsFrameSubscriber.cpp).
        (DataContext as MainViewModel)?.Dispose();
    }

    // Auto-scrolls the scan log box as RunScan appends lines (TextBox has no built-in
    // "stick to bottom" behavior) -- ScrollToEnd on the containing ScrollViewer, not the
    // TextBox itself, since the TextBox has no scrollbar of its own here.
    private void OnScanLogTextChanged(object sender, TextChangedEventArgs e)
    {
        ScanLogScroll.ScrollToEnd();
    }

    // Zoom/pan/reset for the scan-result mosaic view -- see MainWindow.xaml's
    // ResultImageContainer/ResultImage/ResultImageScale/ResultImageTranslate. Kept in
    // code-behind rather than the ViewModel: this is pure view-state (transform values a user
    // drags around), not application state the ViewModel or the offline pipeline needs to know
    // about, so it doesn't belong behind a binding.
    private void OnResultImageMouseWheel(object sender, MouseWheelEventArgs e)
    {
        double factor = e.Delta > 0 ? 1.1 : 1.0 / 1.1;
        double newScale = Math.Clamp(ResultImageScale.ScaleX * factor, MinResultImageZoom, MaxResultImageZoom);
        ResultImageScale.ScaleX = newScale;
        ResultImageScale.ScaleY = newScale;
        e.Handled = true;
    }

    private void OnResultImageMouseDown(object sender, MouseButtonEventArgs e)
    {
        _isPanningResultImage = true;
        _panMouseStart = e.GetPosition(ResultImageContainer);
        _panStartX = ResultImageTranslate.X;
        _panStartY = ResultImageTranslate.Y;
        ResultImageContainer.CaptureMouse();
    }

    private void OnResultImageMouseMove(object sender, MouseEventArgs e)
    {
        if (!_isPanningResultImage)
            return;
        Point pos = e.GetPosition(ResultImageContainer);
        ResultImageTranslate.X = _panStartX + (pos.X - _panMouseStart.X);
        ResultImageTranslate.Y = _panStartY + (pos.Y - _panMouseStart.Y);
    }

    private void OnResultImageMouseUp(object sender, MouseButtonEventArgs e) => StopPanningResultImage();

    private void OnResultImageMouseLeave(object sender, MouseEventArgs e) => StopPanningResultImage();

    private void StopPanningResultImage()
    {
        _isPanningResultImage = false;
        ResultImageContainer.ReleaseMouseCapture();
    }

    private void OnResultImageResetView(object sender, MouseButtonEventArgs e)
    {
        ResultImageScale.ScaleX = 1;
        ResultImageScale.ScaleY = 1;
        ResultImageTranslate.X = 0;
        ResultImageTranslate.Y = 0;
    }

    // Renders the frame's original 640x640 directly into this window's own main content area
    // (SelectedFrameImage/IsShowingSelectedFrame -- see MainWindow.xaml), not a separate popup
    // window (design review: "별도 팝업이 아닙니다"). Double-click only -- same
    // ClickCount-on-MouseLeftButtonDown pattern TransferSettingsWindow's own review panel uses
    // (confirmed via a real test there that checking ClickCount on MouseLeftButtonUp never
    // actually fires); a single click is left alone here since the 선택 checkbox living in the
    // same Border already handles that.
    private void OnCapturedFrameThumbnailClicked(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2)
            return;
        if (sender is not FrameworkElement { Tag: CapturedFrameItem item })
            return;
        if (DataContext is not MainViewModel vm)
            return;

        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new System.Uri(item.FilePath);
            bitmap.EndInit();
            bitmap.Freeze();
            vm.SelectedFrameImage = bitmap;
            vm.IsShowingSelectedFrame = true;
            ResetSelectedFrameView();
        }
        catch (System.Exception ex) when (ex is System.NotSupportedException or System.IO.IOException or System.UnauthorizedAccessException)
        {
            vm.StatusMessage = $"원본 이미지 로드 실패 — {ex.Message}";
        }
    }

    // Zoom/pan/fit for the double-clicked original frame -- same transform-based approach as
    // OnResultImage* above, plus an explicit "화면 맞춤" button (design review specifically asked
    // for a button in addition to whatever gesture resets the view).
    private void OnSelectedFrameMouseWheel(object sender, MouseWheelEventArgs e)
    {
        double factor = e.Delta > 0 ? 1.1 : 1.0 / 1.1;
        double newScale = Math.Clamp(SelectedFrameScale.ScaleX * factor, MinResultImageZoom, MaxResultImageZoom);
        SelectedFrameScale.ScaleX = newScale;
        SelectedFrameScale.ScaleY = newScale;
        e.Handled = true;
    }

    private void OnSelectedFrameMouseDown(object sender, MouseButtonEventArgs e)
    {
        _isPanningSelectedFrame = true;
        _selectedFramePanMouseStart = e.GetPosition(SelectedFrameContainer);
        _selectedFramePanStartX = SelectedFrameTranslate.X;
        _selectedFramePanStartY = SelectedFrameTranslate.Y;
        SelectedFrameContainer.CaptureMouse();
    }

    private void OnSelectedFrameMouseMove(object sender, MouseEventArgs e)
    {
        if (!_isPanningSelectedFrame)
            return;
        Point pos = e.GetPosition(SelectedFrameContainer);
        SelectedFrameTranslate.X = _selectedFramePanStartX + (pos.X - _selectedFramePanMouseStart.X);
        SelectedFrameTranslate.Y = _selectedFramePanStartY + (pos.Y - _selectedFramePanMouseStart.Y);
    }

    private void OnSelectedFrameMouseUp(object sender, MouseButtonEventArgs e) => StopPanningSelectedFrame();

    private void OnSelectedFrameMouseLeave(object sender, MouseEventArgs e) => StopPanningSelectedFrame();

    private void StopPanningSelectedFrame()
    {
        _isPanningSelectedFrame = false;
        SelectedFrameContainer.ReleaseMouseCapture();
    }

    private void OnSelectedFrameFitClick(object sender, RoutedEventArgs e) => ResetSelectedFrameView();

    private void ResetSelectedFrameView()
    {
        SelectedFrameScale.ScaleX = 1;
        SelectedFrameScale.ScaleY = 1;
        SelectedFrameTranslate.X = 0;
        SelectedFrameTranslate.Y = 0;
    }

    // Opens the high-resolution facade image transfer dialog (rsync-over-ssh, separate from
    // this window's own capture/scan flow -- see TransferSettingsWindow). Non-modal (Show, not
    // ShowDialog) so the operator can keep an eye on/use the main capture window while a large
    // transfer runs in the background.
    private void OnOpenTransferSettings(object sender, RoutedEventArgs e)
    {
        // Reuses the same DDS discovery settings (Host/Port/로컬 인터페이스) the operator already
        // entered for the live video subscription above -- the storage-status client
        // (Feedback/Result/CancelRequest) needs to reach the same MngData/DDS-Router host, so
        // there is no separate set of connection fields to fill in on this dialog.
        var vm = DataContext as MainViewModel;
        var window = new TransferSettingsWindow(vm?.DdsRouterHost ?? "", vm?.DdsRouterPort ?? 7410, vm?.LocalInterfaceIp ?? "")
            { Owner = this };
        window.Show();
    }
}